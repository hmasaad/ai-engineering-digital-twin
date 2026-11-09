export async function placeOrder(cart: Array<{ sku: string; qty: number }>) {
  const response = await fetch("/api/checkout", {
    method: "POST",
    body: JSON.stringify(cart),
  });
  // TODO: no timeout and no client-side retry budget
  return response.json();
}
