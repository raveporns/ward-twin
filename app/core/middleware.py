class JsonUtf8CharsetMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_utf8_charset(message):
            if message["type"] == "http.response.start":
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-type":
                        content_type = value.decode("latin-1")
                        parts = content_type.split(";")
                        media_type = parts[0].strip()
                        normalized_media_type = media_type.lower()

                        if (
                            normalized_media_type == "application/json"
                            or normalized_media_type.endswith("+json")
                        ):
                            parameters = [
                                parameter.strip()
                                for parameter in parts[1:]
                                if parameter.strip()
                                and parameter.partition("=")[0]
                                .strip()
                                .lower() != "charset"
                            ]
                            content_type = "; ".join(
                                [media_type, *parameters, "charset=utf-8"]
                            )
                            value = content_type.encode("latin-1")

                    headers.append((name, value))

                message = {**message, "headers": headers}

            await send(message)

        await self.app(scope, receive, send_with_utf8_charset)
