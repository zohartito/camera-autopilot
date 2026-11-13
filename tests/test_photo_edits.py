from pathlib import Path
import sys
import pytest
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from edits import edit_image


def test_edit_preserves_source_and_applies_linear_exposure(tmp_path):
    source = tmp_path / 'source.jpg'
    Image.new('RGB', (100, 80), (100, 100, 100)).save(source)
    before = source.read_bytes()
    result = edit_image(tmp_path, source.name, 1, [0, 0, .5, .5])
    assert source.read_bytes() == before
    with Image.open(tmp_path / result['image']) as output:
        assert output.size == (50, 40)
        assert 136 <= output.getpixel((20, 20))[0] <= 140
    assert (tmp_path / result['image']).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('image,ev,crop', [('../outside.jpg',0,None), ('x.jpg',4,None), ('x.jpg',float('nan'),None), ('x.jpg',0,[0,0,1,2]), ('x.jpg',0,[0,0,0,1])])
def test_rejects_bad_requests(tmp_path, image, ev, crop):
    Image.new('RGB',(100,100)).save(tmp_path / 'x.jpg')
    with pytest.raises(ValueError): edit_image(tmp_path,image,ev,crop)


def test_rejects_symlink_and_spoofed_format(tmp_path):
    Image.new('RGB',(100,100)).save(tmp_path / 'source.jpg')
    (tmp_path / 'link.jpg').symlink_to(tmp_path / 'source.jpg')
    with pytest.raises(ValueError): edit_image(tmp_path,'link.jpg')
    Image.new('RGB',(100,100)).save(tmp_path / 'spoof.jpg',format='PNG')
    with pytest.raises(ValueError): edit_image(tmp_path,'spoof.jpg')


def test_crop_uses_visible_orientation_and_strips_private_exif(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = 'Private photographer'
    Image.new('RGB',(100,80)).save(tmp_path / 'portrait.jpg', exif=exif)
    result = edit_image(tmp_path,'portrait.jpg',crop=[0,0,.5,.5])
    with Image.open(tmp_path / result['image']) as output:
        assert output.size == (40,50)
        assert not output.getexif()
