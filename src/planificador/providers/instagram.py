"""Instagram professional account provider via Facebook Login (SPEC §5, §7, §10).

STUB created by the foundation agent so imports resolve. TODO(Instagram agent): implement.
"""

from __future__ import annotations

from planificador.providers.base import FormatSpec, Provider, PublishContext, StepResult


class InstagramProvider(Provider):
    network = "instagram"
    display_name = "Instagram"
    formats = [
        FormatSpec("post", "Publicación", "images_or_video"),
        FormatSpec("reel", "Reel", "video"),
        FormatSpec("story", "Historia", "image_or_video"),
    ]
    supports_comments = True

    def publish_step(self, ctx: PublishContext) -> StepResult:
        raise NotImplementedError("InstagramProvider.publish_step")
