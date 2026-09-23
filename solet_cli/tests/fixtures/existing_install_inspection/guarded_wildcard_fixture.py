import forbidden_module


def resolve(value: object) -> None:
    match value:
        case _ if False:
            return
    forbidden_module.target_adapter()
