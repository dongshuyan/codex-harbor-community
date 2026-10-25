"""Versions whose local organization/catalog semantics were exercised in a lab.

Schema compatibility alone cannot establish unchanged visibility/migration rules.
An unverified desktop must not receive a speculative shared-state projection.
"""
from pathlib import Path
import plistlib

from core import APP,UserError

VERIFIED_DESKTOPS={('26.1021.31345','21828')}


def require_supported(app=None):
    path=Path(app or APP)/'Contents/Info.plist'
    try:
        value=plistlib.loads(path.read_bytes())
        version=(str(value.get('CFBundleShortVersionString','')),str(value.get('CFBundleVersion','')))
    except (OSError,ValueError,plistlib.InvalidFileException) as exc:
        raise UserError('无法核对原生 Codex 版本，未写入共享项目配置。') from exc
    if version not in VERIFIED_DESKTOPS:
        raise UserError('此原生 Codex 版本尚未通过共享项目兼容验证；未写入共享配置。已有窗口可继续使用，请先更新 Harbor 适配。')
    return version
