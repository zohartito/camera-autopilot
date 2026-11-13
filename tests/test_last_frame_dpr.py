"""The last-frame preview renders at devicePixelRatio, not CSS pixels (soft on 3x iPhones).

Camera-free: workflows.js runs in node against a recording 2D-context fake, like
tests/test_phone_ui_wave0.py. What each test would catch if the fix were reverted is in its name.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "backend" / "web"
WORKFLOWS = (WEB / "workflows.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")

HARNESS = """
const assert=require('node:assert/strict');
let recorded=[];
function context2d() {
  return {
    fillStyle:'', strokeStyle:'', lineWidth:1,
    fillRect(...a){recorded.push(['fillRect',...a]);},
    drawImage(_img,x,y,w,h){recorded.push(['drawImage',x,y,w,h]);},
    getImageData(x,y,w,h){
      recorded.push(['getImageData',x,y,w,h]);
      const data=new Uint8ClampedArray(w*h*4);
      for (let i=0;i<data.length;i+=4){data[i]=data[i+1]=data[i+2]=40;data[i+3]=255;}
      for (let i=0;i<w*4;i+=4){data[i]=data[i+1]=data[i+2]=255;}  // first row clipped
      return {data};
    },
    createImageData(w,h){return {data:new Uint8ClampedArray(w*h*4)};},
    putImageData(){},
    save(){}, restore(){}, beginPath(){}, rect(){}, clip(){},
    moveTo(x,y){recorded.push(['moveTo',x,y]);}, lineTo(x,y){recorded.push(['lineTo',x,y]);},
    stroke(){recorded.push(['stroke',this.lineWidth]);}
  };
}
const document={createElement(){return {width:0,height:0,getContext:context2d};}};
const window={};
""" + WORKFLOWS + """
const W=window.AutopilotWorkflows;
function paint(pixelRatio, histogramClientWidth=358, boxWidth=390, boxHeight=844) {
  recorded=[];
  const canvas={width:0,height:0,getContext:context2d};
  const histogram={width:0,height:0,clientWidth:histogramClientWidth,getContext:context2d};
  const measured=W.paintPreview(canvas,histogram,{naturalWidth:720,naturalHeight:480},{
    grid:true,zebra:true,fit:'contain',boxWidth,boxHeight,pixelRatio});
  return {canvas,histogram,measured,ops:recorded.slice()};
}
"""


def _node() -> str:
    path = shutil.which("node")
    if path is None:
        pytest.skip("node is not on PATH")
    if subprocess.run([path, "--version"], check=False, capture_output=True).returncode != 0:
        pytest.skip("node --version failed")
    return path


def run_js(code: str) -> str:
    result = subprocess.run([_node(), "-e", HARNESS + code], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1500:]
    return result.stdout


def test_a_3x_screen_gets_a_3x_backing_store_and_the_histogram_reads_every_device_pixel():
    out = run_js("""
    const {canvas,measured,ops}=paint(3);
    assert.equal(canvas.width,1170); assert.equal(canvas.height,2532);
    assert.equal(measured.pixelRatio,3);
    assert.deepEqual(measured.dest,W.containedDrawRect(1170,2532,720,480));
    assert.deepEqual(measured.dest,{x:0,y:876,width:1170,height:780,scale:1170/720});
    const read=ops.find(op=>op[0]==='getImageData');
    assert.deepEqual(read,['getImageData',0,876,1170,780]);
    assert.equal(measured.histogram.reduce((a,b)=>a+b),1170*780);
    assert.equal(measured.histogram[255],1170);
    process.stdout.write('ok');
    """)
    assert out == "ok"


def test_grid_lines_are_one_device_pixel_on_pixel_centres():
    out = run_js("""
    const {measured,ops}=paint(3);
    const strokes=ops.filter(op=>op[0]==='stroke');
    assert.deepEqual(strokes,[['stroke',1]]);
    const d=measured.dest;
    const vertical=ops.filter(op=>op[0]==='moveTo' && op[2]===d.y).map(op=>op[1]);
    const horizontal=ops.filter(op=>op[0]==='moveTo' && op[1]===d.x).map(op=>op[2]);
    assert.deepEqual(vertical,[390.5,780.5]);
    assert.deepEqual(horizontal,[876+260.5,876+520.5]);
    for (const op of ops.filter(op=>op[0]==='moveTo' || op[0]==='lineTo')) {
      assert.ok(op[1]>=d.x && op[1]<=d.x+d.width && op[2]>=d.y && op[2]<=d.y+d.height, String(op));
    }
    process.stdout.write('ok');
    """)
    assert out == "ok"


def test_pixel_ratio_is_capped_at_3_and_missing_or_low_ratios_stay_at_css_pixels():
    out = run_js("""
    assert.equal(paint(4).canvas.width,1170);
    assert.equal(paint(2).canvas.width,780);
    for (const ratio of [undefined,null,0,0.5,NaN,'x',1]) assert.equal(paint(ratio).canvas.width,390,String(ratio));
    assert.equal(W.canvasPixelRatio(2.625),2.625);
    process.stdout.write('ok');
    """)
    assert out == "ok"


def test_zebra_stripes_keep_a_12_css_pixel_period_on_dense_screens():
    out = run_js("""
    assert.equal(W.zebraAt(0,0,255,255,255),true);
    assert.equal(W.zebraAt(6,0,255,255,255),false);
    assert.equal(W.zebraAt(10,0,255,255,255,250,3),true);  // 1x period would say false
    assert.equal(W.zebraAt(17,0,255,255,255,250,3),true);
    assert.equal(W.zebraAt(18,0,255,255,255,250,3),false);
    assert.equal(W.zebraAt(36,0,255,255,255,250,3),true);
    assert.equal(W.zebraAt(0,0,10,10,10,250,3),false);
    process.stdout.write('ok');
    """)
    assert out == "ok"


def test_histogram_canvas_is_drawn_at_device_pixels_across_its_css_width():
    out = run_js("""
    const {histogram,ops}=paint(3,358);
    assert.equal(histogram.width,1074); assert.equal(histogram.height,240);
    const background=ops.find(op=>op[0]==='fillRect' && op[3]===1074 && op[4]===240);
    assert.ok(background, 'histogram background covers the device-pixel canvas');
    const unscaled=paint(1,358).histogram;
    assert.equal(unscaled.width,256); assert.equal(unscaled.height,80);
    process.stdout.write('ok');
    """)
    assert out == "ok"


def test_the_last_frame_page_passes_the_screen_ratio_and_keeps_the_css_box():
    render = INDEX.split("function renderLastFrameAids() {", 1)[1].split("\n  }\n", 1)[0]
    assert "pixelRatio: window.devicePixelRatio" in render
    assert "boxWidth: canvas.clientWidth" in render and "boxHeight: canvas.clientHeight" in render
    css = INDEX.split(".result #last-frame-canvas {", 1)[1].split("}", 1)[0]
    assert "width: 100%" in css and "height: 100%" in css
    # workflows.js changed: the page must request the new version (2026-09-17 lesson).
    assert '<script src="/workflows.js?v=20261003c"></script>' in INDEX


def test_the_backing_store_stays_within_the_pixel_budget_on_large_hidpi_viewports():
    """Review of #69: a 3840x2160 CSS viewport at 2x would otherwise allocate 7680x4320 (33 MP) plus
    full-size read, overlay and layer buffers. The canvas and every pixel read stay within 4 MP."""
    out = run_js("""
    assert.equal(W.PREVIEW_MAX_PIXELS,4000000);
    const big=paint(2,358,3840,2160);
    assert.ok(big.canvas.width*big.canvas.height<=W.PREVIEW_MAX_PIXELS, big.canvas.width+'x'+big.canvas.height);
    assert.ok(big.measured.pixelRatio<1 && big.measured.pixelRatio>0.6, String(big.measured.pixelRatio));
    assert.equal(big.canvas.width,Math.floor(3840*big.measured.pixelRatio+1e-9));
    const read=big.ops.find(op=>op[0]==='getImageData');
    assert.ok(read[3]*read[4]<=W.PREVIEW_MAX_PIXELS);
    // An iPad Pro 12.9 box at 2x (5.6 MP) is lowered to the budget; the iPhone box keeps a full 3x.
    const pad=paint(2,358,1024,1366);
    assert.ok(pad.canvas.width*pad.canvas.height<=W.PREVIEW_MAX_PIXELS && pad.measured.pixelRatio<2);
    const phone=paint(3);
    assert.equal(phone.canvas.width,1170); assert.equal(phone.canvas.height,2532); assert.equal(phone.measured.pixelRatio,3);
    // Within budget the grid is still one canvas (= device) pixel on pixel centres.
    assert.deepEqual(phone.ops.filter(op=>op[0]==='stroke'),[['stroke',1]]);
    assert.equal(W.boxPixelRatio(0,0,3),3);
    process.stdout.write('ok');
    """)
    assert out == "ok"
