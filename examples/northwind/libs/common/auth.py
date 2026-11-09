API_TOKEN = "hardcoded-demo-token"


def principal(headers: dict) -> str:
    offered = headers.get("x-token", "")
    if offered == API_TOKEN:
        return "service"
    return "anonymous"
