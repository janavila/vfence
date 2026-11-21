import shutil
import subprocess
from pathlib import Path

import pytest


def test_gateway_event_retention_and_collar_storage_cpp(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("host C++ compiler unavailable")
    root = Path(__file__).resolve().parents[1]
    collar = root / "geofence/VFenceCollarLoRa"
    gateway = root / "gateway/VFenceGateway"
    executable = tmp_path / "event_fence_commit"
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
                    "-I", str(collar), "-I", str(gateway), str(root / "tests/cpp/test_event_and_fence_commit.cpp"),
                    str(collar / "vfence_protocol.cpp"), str(collar / "vfence_fence_transfer.cpp"),
                    "-o", str(executable)], check=True)
    subprocess.run([executable], check=True)


def test_production_lora_profile_transition_cpp(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("host C++ compiler unavailable")
    root = Path(__file__).resolve().parents[1]
    gateway = root / "gateway/VFenceGateway"
    executable = tmp_path / "production_profile"
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-I", str(gateway),
                    "-I", str(root / "firmware_common"), str(root / "tests/cpp/test_live_profile_transition.cpp"),
                    str(gateway / "vfence_protocol.cpp"), "-o", str(executable)], check=True)
    subprocess.run([executable], check=True)


@pytest.mark.parametrize("sketch_path", ("geofence/VFenceCollarLoRa", "gateway/VFenceGateway"))
def test_staged_sketch_includes_shared_headers_once(tmp_path, sketch_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("host C++ compiler unavailable")
    root = Path(__file__).resolve().parents[1]
    sketch = root / sketch_path
    for header in ("lora_profiles.h", "vfence_protocol.h"):
        shutil.copyfile(sketch / header, tmp_path / header)

    source = tmp_path / "sketch.cpp"
    source.write_text(
        '#include "lora_profiles.h"\n'
        '#include "vfence_protocol.h"\n'
        '#include "../../firmware_common/profile_transition.h"\n'
        'static_assert(vfence::LORA_PROFILE_COUNT == 4, "profile catalog");\n'
        'static_assert(vfence::HEADER_SIZE == 5, "wire header");\n',
        encoding="utf-8",
    )
    subprocess.run(
        [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
         "-I", str(sketch), "-fsyntax-only", str(source)],
        check=True,
    )
