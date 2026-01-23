from meshagent.cli import async_typer

from meshagent.api import SchemaRegistry, SchemaRegistration


import logging

app = async_typer.AsyncTyper(help="Developer helper services")


@app.async_command("service", help="Run local helper HTTP services")
async def helpers_service():
    """Run local helper services (agents, schemas, toolkits)."""
    from meshagent.tools.storage import StorageToolkit
    from meshagent.api.services import ServiceHost

    from meshagent.agents.schemas.gallery import gallery_schema
    from meshagent.agents.schemas.document import document_schema
    from meshagent.agents.schemas.transcript import transcript_schema
    from meshagent.agents.schemas.super_editor_document import (
        super_editor_document_schema,
    )
    from meshagent.agents.schemas.presentation import presentation_schema
    from meshagent.agents import thread_schema
    from meshagent.agents.widget_schema import widget_schema

    logging.getLogger("openai").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.ERROR)

    service = ServiceHost(port=9000)

    @service.path("/schemas/document")
    class DocumentSchemaRegistry(SchemaRegistry):
        def __init__(self):
            name = "document"
            schema = document_schema
            super().__init__(
                name=f"meshagent.schema.{name}",
                validate_webhook_secret=False,
                schemas=[SchemaRegistration(name=name, schema=schema)],
            )

    @service.path("/schemas/superdoc")
    class SuperdocDocumentSchemaRegistry(SchemaRegistry):
        def __init__(self):
            name = "superdoc"
            schema = super_editor_document_schema
            super().__init__(
                name=f"meshagent.schema.{name}",
                validate_webhook_secret=False,
                schemas=[SchemaRegistration(name=name, schema=schema)],
            )

    @service.path("/schemas/gallery")
    class GalleryDocumentSchemaRegistry(SchemaRegistry):
        def __init__(self):
            name = "gallery"
            schema = gallery_schema
            super().__init__(
                name=f"meshagent.schema.{name}",
                validate_webhook_secret=False,
                schemas=[SchemaRegistration(name=name, schema=schema)],
            )

    @service.path("/schemas/thread")
    class ThreadDocumentSchemaRegistry(SchemaRegistry):
        def __init__(self):
            name = "thread"
            schema = thread_schema
            super().__init__(
                name=f"meshagent.schema.{name}",
                validate_webhook_secret=False,
                schemas=[SchemaRegistration(name=name, schema=schema)],
            )

    @service.path("/schemas/widget")
    class WidgetDocumentSchemaRegistry(SchemaRegistry):
        def __init__(self):
            name = "widget"
            schema = widget_schema
            super().__init__(
                name=f"meshagent.schema.{name}",
                validate_webhook_secret=False,
                schemas=[SchemaRegistration(name=name, schema=schema)],
            )

    @service.path("/schemas/presentation")
    class PresentationDocumentSchemaRegistry(SchemaRegistry):
        def __init__(presentation):
            name = "presentation"
            schema = presentation_schema
            super().__init__(
                name=f"meshagent.schema.{name}",
                validate_webhook_secret=False,
                schemas=[SchemaRegistration(name=name, schema=schema)],
            )

    @service.path("/schemas/transcript")
    class TranscriptRegistry(SchemaRegistry):
        def __init__(self):
            name = "transcript"
            schema = transcript_schema
            super().__init__(
                name=f"meshagent.schema.{name}",
                validate_webhook_secret=False,
                schemas=[SchemaRegistration(name=name, schema=schema)],
            )

    @service.path("/toolkits/storage")
    class HostedStorageToolkit(StorageToolkit):
        pass

    await service.run()
