"""Floor plan -> 3D digital twin pipeline.

Package layout:
    twin.scene    scene model (source of truth), validation, geometry compiler, transforms
    twin.assets   furniture catalog shared by the AI schema, compiler and Blender library
    twin.ai       AI interpretation service (OpenAI-compatible Responses / Chat Completions)
    twin.imaging  upload normalisation (PNG/JPG/PDF -> PNG)
    twin.blender  headless Blender worker (runner + scripts executed inside Blender)
    twin.unreal   Unreal Engine 5 import (runner + script executed inside the editor)
    twin.api      FastAPI backend, storage and background jobs
"""

__version__ = "0.1.0"
