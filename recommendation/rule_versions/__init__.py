"""
Every released version of the recommendation rules, one module each.

`recommendation.rules` loads the one named by RULES_VERSION (default: the
latest) and checks every module here at startup. Three conventions keep a
rollback honest, and the loader enforces what it can of them:

1. **A version is frozen once an analysis is stored under it.** Its
   rules_version is how that result is explained later, so the module that
   produced it must still produce it. A change is a new module, never an edit.

2. **Every change in behaviour is a switch in these files, not only code.** If
   v11 adds logic to the engine and v11 has to be rolled back, setting
   RULES_VERSION to v10 only works if v10's module can switch that logic off.
   So every module carries every key; an older one carries the value that
   disables what came after it (v9 has `maintenance.min_treatments = 0`). The
   loader refuses to start when two modules disagree on their keys.

3. **Only the current and the previous version need to stay runnable.** That is
   all a rollback needs. When a version two steps back stops mattering, the
   switches that exist only for it can be removed from the engine together with
   its module — otherwise the engine accumulates branches for versions nobody
   will run again.

A module is named after its version with dashes as underscores
(`2026-10-v10` -> `v2026_10_v10.py`) and exposes one dict, `RULES`.
"""
