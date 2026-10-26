"""Select an authorized image destination and keep product writes on it."""

import re


class ImageScopeError(ValueError):
    def __init__(self, message, status_code=403):
        super().__init__(message)
        self.status_code = status_code


def upload_name(value):
    return isinstance(value, str) and re.fullmatch(
        r"[a-z0-9-]+__upload_media", value,
    ) is not None


def choose_upload_tool(tools, selected=None):
    names = {tool.name for tool in tools if upload_name(tool.name)}
    if selected is not None:
        if not upload_name(selected):
            raise ImageScopeError("Choose a valid image destination.", 400)
        if selected not in names:
            raise ImageScopeError(
                "Ask your admin to grant image-upload access for this store.",
            )
        return selected
    if not names:
        raise ImageScopeError("Ask your admin to grant image-upload access.")
    if len(names) != 1:
        raise ImageScopeError(
            "Choose which store should receive this image.", 409,
        )
    return next(iter(names))


def image_tools(tools, selected):
    choose_upload_tool(tools, selected)
    server = selected.partition("__")[0]
    return [
        tool for tool in tools
        if tool.name.partition("__")[0] == server
        and tool.name != selected
    ]


def check_image_call(name, arguments, selected, image_id):
    if name.partition("__")[0] != selected.partition("__")[0]:
        raise ImageScopeError(
            "This image belongs to a different store. No action was run.", 409,
        )
    suffix = name.partition("__")[2]
    if suffix == "upload_media":
        raise ImageScopeError("This image is already uploaded.", 409)
    if suffix in {"create_product", "update_product"} and (
        arguments.get("image_id") != image_id
        or type(arguments.get("image_id")) is not int
        or arguments.get("image_url")
    ):
        raise ImageScopeError(
            "Use the attached image from the selected store. No product was changed.",
            409,
        )
