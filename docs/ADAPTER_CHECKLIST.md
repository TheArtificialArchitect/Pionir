# Specialist adapter checklist

Complete this once per source project before writing its production adapter.

## Identity

- Repository and branch:
- Local path (documentation only; never hard-code it):
- Maintainer:
- Intended Pionir role:
- Source revision audited:

## Runtime

- Language and version:
- Entry point and shutdown path:
- IPC protocol, bind address, and port:
- Authentication mechanism:
- Health/readiness endpoint:
- Required environment variables (names only):

## Models and resources

- Model identifiers and quantization:
- Runtime (Ollama, llama.cpp, Transformers, other):
- Idle/warm RAM:
- Idle/warm VRAM:
- Context limit and measured context overhead:
- Cold-start and warm-task latency:
- CPU/GPU concurrency behavior:

## State and learning

- Databases and schemas:
- Filesystem state:
- Memory namespaces requested:
- Transcript or identity data present:
- Learning loop and promotion mechanism:
- Backup, migration, and rollback procedure:

## Capabilities and safety

- Declared capabilities:
- Tools and side effects:
- Required permissions:
- Approval boundaries:
- Timeout and cancellation behavior:
- Deterministic postconditions:
- Existing tests and evaluation sets:

## Adapter acceptance

- Contract tests pass.
- Secrets and private state are excluded from logs and Git.
- Unavailable service fails closed with a useful error.
- Cancellation releases CPU, GPU, file, and network resources.
- Repeated requests are idempotent where required.
- Source project remains independently runnable.
