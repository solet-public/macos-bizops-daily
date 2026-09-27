#!/usr/bin/env python3
"""Verify package resources from an installed plugin, without source-path injection."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import struct
from importlib import metadata, resources
from zipfile import ZipFile

WHEEL_NAME = 'apple_fm_sdk-0.2.1-py3-none-macosx_27_0_arm64.whl'
WHEEL_SHA = 'e005f63d275935ed3fbdccff6b5ab4d185116f8cbca22e04a5dd5511a2c739e6'


def check_record(archive: ZipFile) -> None:
    rows = list(csv.reader(io.StringIO(archive.read('apple_fm_sdk-0.2.1.dist-info/RECORD').decode())))
    assert len({name for name, _, _ in rows}) == len(rows)
    assert {name for name, _, _ in rows} == set(archive.namelist())
    for name, digest, size in rows:
        if name.endswith('/RECORD'):
            assert digest == size == ''
            continue
        data = archive.read(name)
        actual = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
        assert digest == 'sha256=' + actual, name
        assert int(size) == len(data), name


def check_native_wheel() -> None:
    wheel = resources.files('macos_inference_plugin.vendor').joinpath(WHEEL_NAME).read_bytes()
    assert hashlib.sha256(wheel).hexdigest() == WHEEL_SHA
    with ZipFile(io.BytesIO(wheel)) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist()))
        info = archive.read('apple_fm_sdk-0.2.1.dist-info/WHEEL').decode()
        assert 'Root-Is-Purelib: false' in info
        assert 'Tag: py3-none-macosx_27_0_arm64' in info
        dylib = archive.read('apple_fm_sdk/lib/libFoundationModels.dylib')
        assert dylib[:4] == bytes.fromhex('cffaedfe')
        assert struct.unpack('<I', dylib[4:8])[0] == 0x0100000C
        check_record(archive)


def check_plugin_resources() -> None:
    packaged = resources.files('macos_inference_plugin.resources')
    config = json.loads(packaged.joinpath('default_config.json').read_text())
    assert config['context.warming_enabled'] is False
    assert config['model'] == 'apple-system'
    assert 'name: macos_inference_plugin' in packaged.joinpath('plugin.yaml').read_text()
    dist = metadata.distribution('macos_inference_plugin')
    assert 'apple-fm-sdk==0.2.1' in (dist.requires or [])
    entry = next(ep for ep in dist.entry_points if ep.group == 'ananta.plugins')
    assert entry.name == 'macos_inference_plugin'
    assert entry.value == 'macos_inference_plugin.plugin:Plugin'


def main() -> int:
    check_native_wheel()
    check_plugin_resources()
    print('installed_resources_smoke: package metadata, config, native wheel tag/RECORD/SHA PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
