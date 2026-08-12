"""
يستورد main.py في عملية منفصلة (ببيئة إعدادات مؤقتة) للتأكد أنه يعمل فعلاً.
العملية المنفصلة ضرورية لأن main.py ينفّذ كوداً على مستوى الوحدة ويقرأ إعدادات
حقيقية عند الاستيراد.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "_smoke_main.py")


def test_main_imports_and_pure_helpers_behave(tmp_path):
    env = dict(os.environ)
    env.update({
        "SETTINGS_FILE": str(tmp_path / "settings.json"),
        "API_ID": "12345",
        "API_HASH": "hash",
        "BOT_TOKEN": "token",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    result = subprocess.run(
        [sys.executable, SCRIPT], capture_output=True, text=True, env=env,
        cwd=str(tmp_path), timeout=120,
    )
    assert result.returncode == 0, (
        f"فشل استيراد/فحص main.py:\n{result.stdout}\n{result.stderr}"
    )
    assert "SMOKE OK" in result.stdout
