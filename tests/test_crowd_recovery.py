"""Known-label visibility fixtures; no claim of automatic background recognition."""
from pathlib import Path
import hashlib
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import cv2
import numpy as np
import pytest
import stacking
import crowd_recovery


def fixture_frames(root, visible=1, never=True):
    root.mkdir(exist_ok=True)
    rng=np.random.default_rng(405)
    background=rng.integers(25,225,(97,113,3),dtype=np.uint8)
    yy,xx=np.indices(background.shape[:2])
    groups=(xx//17+yy//19)%4
    images,masks=[],[]
    for index in range(4):
        known=((groups+index)%4)<visible
        if never:known[42:55,50:65]=False
        image=background.copy();image[~known]=[240-index*10,8+index,230]
        path=root/f'frame{index}.png';maskpath=root/f'mask{index}.png'
        assert cv2.imwrite(str(path),image)
        assert cv2.imwrite(str(maskpath),known.astype(np.uint8)*255)
        images.append(path);masks.append(maskpath)
    return background,images,masks


@pytest.mark.parametrize('visible',[1,2,3],ids=['25_percent','50_percent','75_percent'])
def test_recovers_only_known_visible_background_and_marks_never_observed(tmp_path,visible):
    background,images,masks=fixture_frames(tmp_path,visible)
    originals={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in images+masks}
    result=stacking.crowd_remove_guided(images,masks,registered=True)
    known=result.valid_mask==255
    assert np.array_equal(result.image[known],background[known])
    assert np.all(result.support_count[known]==visible)
    assert np.all(result.confidence_map[known]==round(255*visible/4))
    assert np.all(result.never_observed_mask[42:55,50:65]==255)
    assert np.all(result.image[42:55,50:65]==0)
    assert np.all(result.valid_mask[42:55,50:65]==0)
    assert not result.evidence['unseen_pixels_generated']
    assert 'not a probability' in result.evidence['confidence_semantics']
    assert originals=={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in images+masks}


def test_minimum_support_distinguishes_low_confidence_from_never_observed(tmp_path):
    _,images,masks=fixture_frames(tmp_path,1)
    result=crowd_recovery.recover_background(images,masks,registered=True,minimum_observations=2)
    assert not np.any(result.valid_mask)
    assert np.any(result.support_count==1)
    assert np.count_nonzero(result.never_observed_mask)==13*15
    assert not np.any(result.image)


def test_all_occluded_inputs_produce_explicit_unknown_not_background(tmp_path):
    _,images,masks=fixture_frames(tmp_path)
    for mask in masks:
        assert cv2.imwrite(str(mask),np.zeros((97,113),np.uint8))
    result=crowd_recovery.recover_background(images,masks,registered=True)
    assert np.all(result.never_observed_mask==255)
    assert not np.any(result.valid_mask) and not np.any(result.confidence_map)
    assert result.evidence['never_observed_fraction']==1


def test_masks_do_not_silently_realign_with_foreground(tmp_path,monkeypatch):
    background,images,masks=fixture_frames(tmp_path)
    monkeypatch.setattr(stacking,'_registration',lambda *a,**k:pytest.fail('guided inputs were re-registered'))
    result=crowd_recovery.recover_background(images,masks,registered=True)
    assert np.array_equal(result.image[result.valid_mask==255],background[result.valid_mask==255])
    with pytest.raises(ValueError,match='registered=True'):
        crowd_recovery.recover_background(images,masks)


def test_soft_masks_cannot_be_treated_as_known_background(tmp_path):
    _,images,masks=fixture_frames(tmp_path)
    assert cv2.imwrite(str(masks[0]),np.full((97,113),128,np.uint8))
    with pytest.raises(ValueError,match='only 0 and 255'):
        crowd_recovery.recover_background(images,masks,registered=True)


def test_mask_geometry_must_match_exact_source_dimensions(tmp_path,monkeypatch):
    _,images,masks=fixture_frames(tmp_path)
    assert cv2.imwrite(str(masks[0]),np.zeros((24,24),np.uint8))
    monkeypatch.setattr(stacking,'_read_one',lambda *a:pytest.fail('image pixels decoded before mask geometry check'))
    with pytest.raises(ValueError,match='dimensions'):
        crowd_recovery.recover_background(images,masks,registered=True)


def test_missing_or_extra_mask_is_rejected(tmp_path):
    _,images,masks=fixture_frames(tmp_path)
    with pytest.raises(ValueError,match='one background mask'):
        crowd_recovery.recover_background(images,masks[:-1],registered=True)


def test_mask_symlink_is_not_followed(tmp_path):
    _,images,masks=fixture_frames(tmp_path)
    link=tmp_path/'link.png';link.symlink_to(masks[0])
    with pytest.raises(OSError):
        crowd_recovery.recover_background(images,[link,*masks[1:]],registered=True)


def test_tiled_guided_result_matches_whole_image_exactly(tmp_path,monkeypatch):
    _,images,masks=fixture_frames(tmp_path,2)
    original=crowd_recovery.recover_background(images,masks,registered=True)
    monkeypatch.setattr(stacking,'STACK_TILE_SIZE',32)
    tiled=crowd_recovery.recover_background(images,masks,registered=True)
    for name in ['image','support_count','confidence_map','valid_mask','never_observed_mask']:
        assert np.array_equal(getattr(original,name),getattr(tiled,name))


def test_guided_mask_aggregate_guard_precedes_decode(tmp_path,monkeypatch):
    _,images,masks=fixture_frames(tmp_path)
    monkeypatch.setattr(crowd_recovery,'MAX_GUIDED_MASK_BYTES',1)
    monkeypatch.setattr(crowd_recovery,'_read_mask',lambda *a:pytest.fail('mask decode before aggregate guard'))
    with pytest.raises(ValueError,match='aggregate temporary-mask'):
        crowd_recovery.recover_background(images,masks,registered=True)


def test_bundle_requires_all_uncertainty_maps_and_preserves_existing_files(tmp_path):
    _,images,masks=fixture_frames(tmp_path/'source')
    result=crowd_recovery.recover_background(images,masks,registered=True)
    root=result.save_bundle(tmp_path/'bundle')
    expected={'recovered.png','support_count.png','confidence_support.png','valid_mask.png','never_observed_mask.png','evidence.json'}
    assert {p.name for p in root.iterdir()}==expected
    assert root.stat().st_mode&0o777==0o700
    assert all(p.stat().st_mode&0o777==0o600 for p in root.iterdir())
    evidence=json.loads((root/'evidence.json').read_text())
    assert set(evidence['outputs'])==expected-{'evidence.json'}
    before={p.name:p.read_bytes() for p in root.iterdir()}
    with pytest.raises(FileExistsError):result.save_bundle(root)
    assert before=={p.name:p.read_bytes() for p in root.iterdir()}


def test_cli_produces_inspectable_bundle_without_camera(tmp_path,capsys):
    _,images,masks=fixture_frames(tmp_path/'source')
    args=['--registered','--output',str(tmp_path/'bundle')]
    for image,mask in zip(images,masks):args+=['--frame',str(image),str(mask)]
    assert crowd_recovery.main(args)==0
    output=json.loads(capsys.readouterr().out)
    assert Path(output['evidence']).is_file()


def test_guidance_recovers_minority_background_that_plain_median_cannot(tmp_path):
    background,images,masks=fixture_frames(tmp_path,1,never=False)
    median=stacking.crowd_remove(images)
    guided=crowd_recovery.recover_background(images,masks,registered=True)
    assert np.abs(median.astype(np.int16)-background.astype(np.int16)).mean()>10
    assert np.array_equal(guided.image,background)


def test_support_fraction_does_not_pretend_to_verify_false_caller_labels(tmp_path):
    background,images,masks=fixture_frames(tmp_path,1,never=False)
    for mask in masks:
        assert cv2.imwrite(str(mask),np.full((97,113),255,np.uint8))
    result=crowd_recovery.recover_background(images,masks,registered=True)
    assert np.all(result.confidence_map==255)
    assert not np.array_equal(result.image,background)
    assert result.evidence['background_labels']=='caller_asserted_known_visible_background_not_automatically_verified'


def test_standalone_cli_wrapper_exports_private_bundle(tmp_path):
    import subprocess
    _,images,masks=fixture_frames(tmp_path/'source')
    cli=Path(__file__).resolve().parents[1]/'tools/recover_crowd_background.py'
    args=[sys.executable,str(cli),'--registered','--output',str(tmp_path/'bundle')]
    for image,mask in zip(images,masks):args+=['--frame',str(image),str(mask)]
    result=subprocess.run(args,capture_output=True,text=True,timeout=30,check=True)
    receipt=json.loads(result.stdout)
    assert Path(receipt['evidence']).is_file()
    rejected=subprocess.run(args,capture_output=True,text=True,timeout=30)
    assert rejected.returncode!=0 and 'output already exists' in rejected.stderr


@pytest.mark.parametrize('orientation',[2,3,4,5,6,7,8])
def test_nonidentity_exif_orientation_rejected_before_guided_decode(tmp_path,monkeypatch,orientation):
    from PIL import Image
    rgb=np.zeros((64,96,3),np.uint8);rgb[:,:48]=(220,20,20);rgb[:,48:]=(20,220,20)
    image=Image.fromarray(rgb);exif=image.getexif();exif[274]=orientation
    paths=[];masks=[]
    for index in range(2):
        path=tmp_path/f'orientation-{index}.jpg';image.save(path,exif=exif)
        mask=tmp_path/f'mask-{index}.png';pixels=np.zeros((64,96),np.uint8);pixels[:,:48]=255
        assert cv2.imwrite(str(mask),pixels)
        paths.append(path);masks.append(mask)
    monkeypatch.setattr(stacking,'_read_one',lambda *a:pytest.fail('oriented source reached decoder'))
    with pytest.raises(ValueError,match='identity EXIF orientation'):
        crowd_recovery.recover_background(paths,masks,registered=True)


def test_oriented_mask_cannot_change_registered_pixel_meaning(tmp_path):
    from PIL import Image
    _,images,masks=fixture_frames(tmp_path)
    image=Image.fromarray(np.full((97,113),255,np.uint8));exif=image.getexif();exif[274]=3
    image.save(masks[0],exif=exif)
    with pytest.raises(ValueError,match='masks require identity EXIF orientation'):
        crowd_recovery.recover_background(images,masks,registered=True)


def test_source_aba_swap_cannot_diverge_pixels_from_receipt_hash(tmp_path,monkeypatch):
    images=[];masks=[]
    for index in range(2):
        image=tmp_path/f'image{index}.png';mask=tmp_path/f'mask{index}.png'
        assert cv2.imwrite(str(image),np.full((32,48,3),70,np.uint8))
        assert cv2.imwrite(str(mask),np.full((32,48),255,np.uint8))
        images.append(image);masks.append(mask)
    originals={path:path.read_bytes() for path in images}
    decoder=stacking._read_one
    staged=[]
    def swap_originals_around_decode(path):
        staged.append(Path(path))
        assert Path(path) not in images
        assert Path(path).stat().st_mode&0o777==0o600
        try:
            for original in images:
                assert cv2.imwrite(str(original),np.full((32,48,3),190,np.uint8))
            return decoder(path)
        finally:
            for original,data in originals.items():original.write_bytes(data)
    monkeypatch.setattr(stacking,'_read_one',swap_originals_around_decode)
    result=crowd_recovery.recover_background(images,masks,registered=True)
    assert np.all(result.image==70)
    assert all(receipt['sha256']==hashlib.sha256(originals[Path(receipt['path'])]).hexdigest()
               for receipt in result.evidence['source_receipts'])
    assert staged and all(not path.exists() for path in staged)
    assert result.evidence['source_binding']=='private_encoded_snapshots_with_matching_sha256_and_identity_orientation'
