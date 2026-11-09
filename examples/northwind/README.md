# Northwind Exchange

Northwind Exchange is a small commerce stack: an API gateway, checkout, payments with a retry worker, inventory, and notifications. It is the sample system shipped with the engineering digital twin.

The retry worker, the checkout/inventory import cycle, the shared SQL helper, and the service token are intentionally unhealthy so the twin has something true to find.
