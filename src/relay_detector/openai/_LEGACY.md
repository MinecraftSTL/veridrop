# Legacy OpenAI package

This package backs the existing `relay-detector openai validate` and
`relay-detector openai baseline` CLI commands.

Phase 2 moved the implementation to `relay_detector.protocols.openai`.
This package now remains as a re-export shim for backward compatibility.
