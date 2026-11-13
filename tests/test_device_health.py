import hashlib
import json
import os
from pathlib import Path
import pytest
from backend.device_health import device_health, portable_readiness, validate_offline_manifest


def put(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value))


def package(tmp_path):
    artifact = tmp_path / 'model.bin'
    artifact.write_bytes(b'existing-local-fixture-model')
    manifest = tmp_path / 'offline.json'
    manifest.write_text(json.dumps({'schema': 'camera-offline-artifacts-v1', 'artifacts': [{
        'path': artifact.name, 'size_bytes': artifact.stat().st_size,
        'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}]}))
    token = tmp_path / 'token'
    token.write_text('private-fixture-token-' + 'a' * 30)
    token.chmod(0o600)
    return artifact, manifest, token


def test_unknown_is_null_not_fake_zero(tmp_path):
    report = device_health(tmp_path, tmp_path / 'sys')
    assert report['battery'] is None
    assert report['temperature'] is None
    assert report['power_telemetry_available'] is False
    assert report['free_bytes'] > 0
    assert report['physical_endurance_verified'] is False


def test_linux_battery_temperature_and_invalid_sensor(tmp_path):
    for name, value in {'type': 'Battery', 'capacity': 72, 'status': 'Discharging',
                        'power_now': 12500000}.items():
        put(tmp_path, 'class/power_supply/BAT0/' + name, value)
    put(tmp_path, 'class/thermal/thermal_zone0/temp', 61000)
    put(tmp_path, 'class/thermal/thermal_zone0/type', 'cpu')
    put(tmp_path, 'class/thermal/thermal_zone1/temp', 'nan')
    report = device_health(tmp_path, tmp_path)
    assert report['battery'][0]['percent'] == 72
    assert report['battery'][0]['power_watts'] == 12.5
    assert report['temperature'] == [{'name': 'cpu', 'celsius': 61}]
    assert report['power_telemetry_available']


def test_missing_storage_is_unknown(tmp_path):
    assert device_health(tmp_path / 'missing', tmp_path)['free_bytes'] is None


def test_offline_verified_but_field_endurance_stays_unverified(tmp_path):
    artifact, manifest, token = package(tmp_path)
    report = portable_readiness(tmp_path, token, manifest, tmp_path, 0, tmp_path)
    assert report['ready']
    assert not report['field_ready']
    assert len(report['unknowns']) == 3
    assert 'private-fixture-token' not in json.dumps(report)
    assert not report['offline_artifacts']['downloads_attempted']


@pytest.mark.parametrize('failure', ['missing-model', 'changed-model', 'missing-token', 'public-token', 'symlink-token', 'low-space'])
def test_faults_block_readiness(tmp_path, failure):
    artifact, manifest, token = package(tmp_path)
    minimum = 0
    if failure == 'missing-model': artifact.unlink()
    elif failure == 'changed-model': artifact.write_bytes(b'x' * artifact.stat().st_size)
    elif failure == 'missing-token': token.unlink()
    elif failure == 'public-token': token.chmod(0o644)
    elif failure == 'symlink-token':
        replacement = tmp_path / 'other-token'
        token.rename(replacement)
        token.symlink_to(replacement)
    elif failure == 'low-space': minimum = 2**80
    assert not portable_readiness(tmp_path, token, manifest, tmp_path, minimum, tmp_path)['ready']


@pytest.mark.parametrize('payload', [
    {}, {'schema': 'camera-offline-artifacts-v1', 'artifacts': []},
    {'schema': 'camera-offline-artifacts-v1', 'artifacts': [{'path': '../outside', 'size_bytes': 0, 'sha256': 'a'*64}]},
    {'schema': 'camera-offline-artifacts-v1', 'artifacts': [{'path': '/etc/passwd', 'size_bytes': 0, 'sha256': 'a'*64}]},
    {'schema': 'camera-offline-artifacts-v1', 'artifacts': [{'path': 'x', 'size_bytes': True, 'sha256': 'a'*64}]},
])
def test_malformed_or_escaping_manifest_fails(tmp_path, payload):
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps(payload))
    assert not validate_offline_manifest(manifest, tmp_path)['ready']


def test_manifest_builder_roundtrip_and_missing_or_duplicate_file(tmp_path):
    from deploy.artifact_manifest import build_manifest
    artifact, manifest, token = package(tmp_path)
    payload = build_manifest(tmp_path, [artifact.name])
    manifest.write_text(json.dumps(payload))
    assert validate_offline_manifest(manifest, tmp_path)['ready']
    with pytest.raises(ValueError): build_manifest(tmp_path, [artifact.name, artifact.name])
    with pytest.raises(OSError): build_manifest(tmp_path, ['missing.bin'])


def test_radio_configuration_and_presence_never_imply_active_band(tmp_path):
    from backend.device_health import radio_status
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps({'schema':'camera-portable-host-v1','radio':{
        'band':'5ghz','channel':36,'hardware_support_verified':True,'runtime_access_point_verified':True}}))
    (tmp_path/'class/net/wlan0/wireless').mkdir(parents=True)
    put(tmp_path,'class/net/wlan0/operstate','up')
    result=radio_status(tmp_path,profile)
    assert result['configuration']['band']=='5ghz'
    assert result['configuration']['status']=='configured_only'
    assert not result['configuration']['hardware_support_verified']
    assert result['probe']['interfaces']==[{'name':'wlan0','operstate':'up'}]
    assert result['probe']['supported_bands'] is None
    assert result['runtime']['active_band'] is None
    assert not result['runtime']['access_point_verified']


def test_missing_or_invalid_radio_profile_does_not_claim_configuration(tmp_path):
    from backend.device_health import radio_status
    result=radio_status(tmp_path,tmp_path/'missing.json')
    assert result['configuration'] is None and result['configuration_error']
    assert len(result['renderable_band_choices'])==2
    profile=tmp_path/'bad.json';profile.write_text('[]')
    assert radio_status(tmp_path,profile)['configuration_error']


@pytest.mark.parametrize('kind', ['fifo', 'symlink', 'oversize'])
def test_radio_profile_special_or_oversize_file_rejected(tmp_path, kind):
    from backend.device_health import radio_status
    profile = tmp_path / 'profile.json'
    if kind == 'fifo':
        os.mkfifo(profile)
    elif kind == 'symlink':
        target = tmp_path / 'target.json'
        target.write_text('{}')
        profile.symlink_to(target)
    else:
        profile.write_bytes(b' ' * 65537)
    result = radio_status(tmp_path, profile)
    assert result['configuration'] is None and result['configuration_error']


def test_known_server_identity_distinguishes_process_and_configured_device(monkeypatch):
    from backend.device_health import known_server_identity
    monkeypatch.delenv('CAMERA_AUTOPILOT_DEVICE_ID',raising=False)
    first=known_server_identity()
    assert first['identity_scope']=='backend_process' and first['device_id'] is None
    assert first['process_instance_id']==known_server_identity()['process_instance_id']
    assert not first['automatic_network_discovery'] and first['control_requires_authentication']
    identity='465fe613-e5a7-493d-a089-2275b98310ce'
    monkeypatch.setenv('CAMERA_AUTOPILOT_DEVICE_ID',identity)
    result=known_server_identity()
    assert result['device_id']==identity and result['identity_scope']=='configured_device'
    monkeypatch.setenv('CAMERA_AUTOPILOT_DEVICE_ID','bad-value')
    assert known_server_identity()['configuration_error']
    monkeypatch.setenv('CAMERA_AUTOPILOT_DEVICE_NAME','bad\nname')
    assert known_server_identity()['display_name']=='Camera Autopilot'
