"""The agent chain.

Each module is one step of the pipeline and can be used on its own:

``denoise``     audio clean-up (noisereduce / Facebook DNS64 / passthrough)
``transcribe``  Whisper speech-to-text
``retrieval``   LlamaIndex semantic search over the protocol PDFs
``vagueness``   hypothesis expansion for reports that retrieve badly
``rules``       deterministic keyword triage — the offline safety net
``triage``      LLM triage, merged with the rule engine
``logistics``   live inventory annotation
``pipeline``    the orchestrator that wires the above together

This package intentionally re-exports nothing: the modules import each other,
and keeping ``__init__`` empty removes any chance of a circular import at
start-up.  Import the module you need directly::

    from aria.agents.pipeline import IntakePipeline
"""
