import requests


def send_receipt(to: str, body: str) -> int:
    # TODO: install the internal CA instead of skipping verification
    response = requests.post(
        "https://mail.northwind.internal/send",
        json={"to": to, "body": body},
        verify=False,
        timeout=30,
    )
    return response.status_code
