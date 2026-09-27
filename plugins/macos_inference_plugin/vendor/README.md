# Apple Foundation Models SDK wheel for macOS 27 arm64

`apple_fm_sdk-0.2.1-py3-none-macosx_27_0_arm64.whl` is the local, native
distribution of Apple's official `apple-fm-sdk` 0.2.1. It allows a macOS 27
arm64 install without compiling the upstream source distribution on the target.

| Artifact | SHA-256 |
|---|---|
| [Apple's PyPI source distribution](https://pypi.org/project/apple-fm-sdk/0.2.1/) `apple_fm_sdk-0.2.1.tar.gz` | `e1801d68ae0517f8524c31723b1dc1a662bf9d06b7a9cc765c9dddb77e647980` |
| Host-built prototype `apple_fm_sdk-0.2.1-py3-none-any.whl` | `e734884f22db4c35f74b7e0b4d17d288fb130c04f74020adc0dd757874c838ed` |
| This retagged wheel | `e005f63d275935ed3fbdccff6b5ab4d185116f8cbca22e04a5dd5511a2c739e6` |

The 2026-09-25 prototype artifact was measured before retagging. All 15 SDK
Python source modules inside it match Apple's PyPI source distribution byte for
byte. It contains an arm64 Mach-O `libFoundationModels.dylib` with a macOS 26.0
minimum target, yet its original wheel metadata incorrectly says
`Root-Is-Purelib: true` and `Tag: py3-none-any`. The checked-in
[`retag_apple_fm_wheel.py`](../tools/retag_apple_fm_wheel.py) verifies the
prototype digest and arm64 header, changes those two metadata fields, and
recomputes `RECORD`; the SDK modules and libraries are copied without changes.

Install proof on a physical macOS 27.0 arm64 host with Python 3.13:

```bash
python3.13 -m venv /tmp/apple-fm-wheel-proof
/tmp/apple-fm-wheel-proof/bin/python -m pip install --no-index --no-deps \
  plugins/macos_inference_plugin/vendor/apple_fm_sdk-0.2.1-py3-none-macosx_27_0_arm64.whl
/tmp/apple-fm-wheel-proof/bin/python -c 'import apple_fm_sdk as fm; print(fm.SystemLanguageModel().is_available())'
```

The measured result was `(True, None)` with `context_size == 8192`. The plugin
dependency is pinned to `apple-fm-sdk==0.2.1`; seed/installer integration must
put this wheel on pip's find-links path or install the verified wheel before the
editable plugin install. A default PyPI resolution otherwise selects Apple's
source-only release and requires a local Swift/Xcode build. That integration is
owned by the seed/setup Changes and is not implied by the presence of this
wheel.
