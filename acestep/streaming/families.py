"""Backend family registry.

Maps a ``SessionConfig.backend`` family name to a factory that builds
the session's :class:`~acestep.streaming.generator_backend.GeneratorBackend`.
Adding a model family is an entry here plus a backend module — the
session, runner, wire protocol, and UI do not change (see
``round_3_BACKEND_PLAN_FINAL.md``).

Factory contract: ``factory(streaming_session) -> GeneratorBackend``.
The factory pulls whatever it needs off the (fully constructed)
StreamingSession; that keeps this registry free of per-family argument
plumbing.

A registration dict is deliberate (vs entry points / import scanning):
with a handful of in-tree families, an explicit dict is greppable and
import-cheap. Revisit only if out-of-tree families become real.
"""

from acestep.engine.obs import logger


def _make_acestep(ss):
    from acestep.streaming.ace_backend import ACEStepBackend

    return ACEStepBackend(
        ss.session, ss.stream,
        state=ss.state,
        use_midi=True,  # always "MIDI" mode; KnobState provides values
        use_sde=ss.use_sde, use_lora=ss.use_lora,
        midi_knobs=ss.virtual_knobs,
        engine_obj=ss.engine_obj,
        vae_window=ss.vae_window, crop_seconds=ss.crop_seconds,
        k1_name=ss.k1_name, seed=1528, skip_threshold=5e-4,
        walk_window=ss.walk_window,
        walk_window_s=ss.walk_window_s,
        neg_conditioning=ss.cond_negative,
    )


FAMILIES = {
    "acestep": _make_acestep,
}


def make_backend(name: str, streaming_session):
    """Build the GeneratorBackend for ``name``.

    Unknown families fail loudly at session create (config-time error,
    never a silent fallback): the client asked for a generator this
    server build does not ship.
    """
    try:
        factory = FAMILIES[name]
    except KeyError:
        known = ", ".join(sorted(FAMILIES))
        logger.error("unknown_backend_family name={} known={}", name, known)
        raise ValueError(
            f"unknown backend family {name!r} (registered: {known})"
        ) from None
    return factory(streaming_session)
