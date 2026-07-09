"""
TTSエンジンのレジストリ — 言語→エンジン名の解決（DATA_SCHEMA.md episodes[].locales,
config.tts.locale_engines / Docs/08_i18n.md §4）。
"""
from . import irodori, omnivoice
from .omnivoice import MissingRefAudioError

_ENGINES = {
    "irodori": irodori,
    "omnivoice": omnivoice,
}


def resolve_engine_name(pj: dict, lang: str | None) -> str:
    """言語からエンジン名を解決する。

    lang省略 or 原語 → config.tts.engine（現行動作・後方互換）。
    それ以外 → config.tts.locale_engines[lang] → locale_engines.default → 組み込み既定 "omnivoice"。
    """
    source_lang = pj.get("language", "ja")
    tts_cfg = pj.get("config", {}).get("tts", {})
    if not lang or lang == source_lang:
        return tts_cfg.get("engine", "irodori")
    locale_engines = tts_cfg.get("locale_engines", {})
    return locale_engines.get(lang) or locale_engines.get("default") or "omnivoice"


def get_engine(name: str):
    """エンジン名から実装モジュールを返す（未知の名前は irodori にフォールバック）。"""
    return _ENGINES.get(name, irodori)
