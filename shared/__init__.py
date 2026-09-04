"""Shared infrastructure for the AI generation portal sub-apps.

Kept stdlib-only so every sub-app (seedance / nano-banana / director / ...) can
`from shared import model_gateway` without pulling in third-party dependencies.
"""