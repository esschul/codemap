from dataclasses import dataclass, field as dc_field


@dataclass
class Endpoint:
    http_method: str          # GET / POST / PUT / DELETE / PATCH / ANY
    path: str                 # combined base + method path
    handler: str              # Kotlin method name
    calls: list[str] = dc_field(default_factory=list)        # service class names used in body
    field_calls: list[dict] = dc_field(default_factory=list) # [{field, type, method}] for evidence


@dataclass
class NonHttpEntrypoint:
    kind: str        # SCHEDULED | KAFKA | EVENT
    method: str      # method name
    detail: str      # topic / cron / event type if known


@dataclass
class Component:
    name: str
    kind: str                 # CONTROLLER | SERVICE | REPOSITORY | CLIENT | …
    package: str
    file: str
    base_path: str = ''       # @RequestMapping on the class
    endpoints: list[Endpoint] = dc_field(default_factory=list)
    non_http_entrypoints: list[NonHttpEntrypoint] = dc_field(default_factory=list)
    dependencies: list[str] = dc_field(default_factory=list)     # other component names
    field_map: dict[str, str] = dc_field(default_factory=dict)   # fieldName → TypeName
    external_systems: list[str] = dc_field(default_factory=list)
    spring_annotations: list[str] = dc_field(default_factory=list)
    classification_reason: str = ''   # human-readable: which annotation/suffix triggered kind
    domain: str = ''
    capability: str = ''
    loc: int = 0
