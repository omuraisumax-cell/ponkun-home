# Third-party notices

The audited Python package inventory and installed versions are listed in
[`THIRD_PARTY_PYTHON_LICENSES.md`](./THIRD_PARTY_PYTHON_LICENSES.md). Package
licenses remain separate from the proposed Ponkun Home license.

This file is a release checklist, not legal advice. Verify the exact version and license of every component before distribution.

## Fish Audio

The project calls the Fish Audio Cloud API and contains an independently built proxy. It does not need to include Fish Speech model weights. Fish Speech source and weights are governed by the license shipped with the exact release/model; current upstream releases use the Fish Audio Community License, while some weights (including S2-Pro) may use separate research terms. Do not redistribute Fish Audio source, weights, reference voices, or generated voice assets without confirming the applicable terms. Cloud API use is also subject to Fish Audio service terms.

## Whisper / faster-whisper

OpenAI Whisper is MIT-licensed. `faster-whisper`, CTranslate2, CUDA libraries, and downloaded model weights have their own notices and must be reviewed separately. No model weights should be committed here.

## Qwen 2.5

The official Qwen2.5-32B model card identifies Apache-2.0. Quantized or repackaged artifacts may add their own terms. This repository should store only configuration, never model files.

## StackChan

`StackChan/` is a separate upstream Git repository and is intentionally excluded from the Ponkun Home publishable tree. If the integration is published, reference a verified upstream commit as a documented external dependency or approved submodule; preserve its license and notices.

## Python and JavaScript dependencies

Generate a dependency inventory and license report from the clean release environment. Do not assume that the license of a top-level package covers transitive dependencies or bundled assets.

