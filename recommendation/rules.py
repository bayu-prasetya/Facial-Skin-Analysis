"""
Which version of the recommendation rules is in force.

Every number the engine uses lives in one dict, `RECOMMENDATION_RULES`. Each
released version of that dict is its own module in `rule_versions/`; this
module picks one and exposes it under the same name the engine has always
imported, so nothing that reads the rules has to know they are versioned.

    RULES_VERSION=2026-10-v9   uvicorn main:app      # roll the rules back
    (unset)                                          # the latest version

The variable is read once, at import — changing it takes a restart, not a code
deploy. That is the point of keeping rules apart from code: a recalibration that
turns out wrong can be taken back without also taking back the unrelated fixes
shipped alongside it.

Startup refuses to proceed rather than guess:
  * RULES_VERSION names a version that does not exist;
  * a module's `version` does not match its file name;
  * two modules disagree on their keys. The engine reads every key on every
    request, so an older module missing a key added later would fail mid-request
    — and a key that is present but switched off is exactly how an older version
    turns off behaviour that came after it.

The version is stored on each analysis as rules_version, which is the only way
an old recommendation can still be explained after the rules move on — and why a
version is never edited once an analysis has been stored under it. See
`rule_versions/__init__.py` for the conventions.
"""
import importlib
import os
import pkgutil
import re

from . import rule_versions

_VERSION_PATTERN = re.compile(r"^(\d{4})-(\d{2})-v(\d+)$")


def _sort_key(version: str) -> tuple:
    """2026-10-v10 sorts after 2026-10-v9: compared as numbers, not strings."""
    match = _VERSION_PATTERN.match(version)
    if not match:
        raise RuntimeError(f"Rules version {version!r} is not in the form YYYY-MM-vN.")
    return tuple(int(part) for part in match.groups())


def _shape(value):
    """The keys of every nested dict; leaves are not compared, because an older
    version is allowed — expected — to hold different values."""
    if isinstance(value, dict):
        return {key: _shape(inner) for key, inner in value.items()}
    return None


def _load_all() -> dict:
    found = {}
    for module_info in pkgutil.iter_modules(rule_versions.__path__):
        module = importlib.import_module(f"{rule_versions.__name__}.{module_info.name}")
        rules = getattr(module, "RULES", None)
        if not isinstance(rules, dict):
            raise RuntimeError(f"rule_versions/{module_info.name}.py has no RULES dict.")
        version = rules.get("version", "")
        expected = "v" + version.replace("-", "_")
        if module_info.name != expected:
            raise RuntimeError(
                f"rule_versions/{module_info.name}.py declares version {version!r}; "
                f"its file should be named {expected}.py.")
        _sort_key(version)
        found[version] = rules
    if not found:
        raise RuntimeError("No rules versions found in recommendation/rule_versions/.")
    return found


AVAILABLE_VERSIONS = _load_all()
LATEST_VERSION = max(AVAILABLE_VERSIONS, key=_sort_key)

_reference = _shape(AVAILABLE_VERSIONS[LATEST_VERSION])
for _version, _rules in AVAILABLE_VERSIONS.items():
    if _shape(_rules) != _reference:
        raise RuntimeError(
            f"Rules {_version} does not have the same keys as {LATEST_VERSION}. Every "
            f"version must carry every key, with the value that switches newer "
            f"behaviour off — otherwise rolling back to it fails mid-request.")

_HEALTH_SOURCES = ("raw_mean", "vendor_all")
for _version, _rules in AVAILABLE_VERSIONS.items():
    if _rules["health_source"] not in _HEALTH_SOURCES:
        raise RuntimeError(
            f"Rules {_version}: health_source must be one of {_HEALTH_SOURCES}, "
            f"not {_rules['health_source']!r}.")

_TIE_BREAKS = {"r5", "severity_width", "price", "id"}
for _version, _rules in AVAILABLE_VERSIONS.items():
    _unknown = set(_rules["ranking"]["tie_break"]) - _TIE_BREAKS
    if _unknown:
        raise RuntimeError(f"Rules {_version}: unknown tie_break keys {sorted(_unknown)}.")

_requested = os.environ.get("RULES_VERSION", "").strip() or LATEST_VERSION
if _requested not in AVAILABLE_VERSIONS:
    raise RuntimeError(
        f"RULES_VERSION={_requested!r} is not a known rules version. "
        f"Available: {', '.join(sorted(AVAILABLE_VERSIONS, key=_sort_key))}.")

RECOMMENDATION_RULES = AVAILABLE_VERSIONS[_requested]

LEVEL_ORDER = {"S1": 1, "S2": 2, "S3": 3, "S4": 4}
