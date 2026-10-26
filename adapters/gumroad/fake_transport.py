from adapters.errors import NotFound

class FakeGumroadTransport:
    def __init__(self):
        self.products: dict[str, dict] = {}
        self.sales: list[dict] = []
        self.writes: list[tuple] = []
        self.calls: list[str] = []
        self._faults: dict[str, Exception] = {}

    def inject(self, op: str, exc: Exception) -> None:
        self._faults[op] = exc

    def _enter(self, op: str):
        self.calls.append(op)
        if op in self._faults:
            raise self._faults[op]

    def add_product(self, product_id: str, *, price: int = 1000, short_url: str | None = None) -> None:
        if short_url is None:
            short_url = f"https://gum.test/l/{product_id}"
        self.products[product_id] = {
            "id": product_id,
            "name": f"Product {product_id}",
            "price": price,
            "currency": "usd",
            "published": True,
            "short_url": short_url,
        }

    def ping(self):
        self._enter("ping")
        return {"user": {}}

    def get_products(self):
        self._enter("get_products")
        return list(self.products.values())

    def get_product(self, product_id: str):
        self._enter("get_product")
        if product_id not in self.products:
            raise NotFound(product_id)
        return self.products[product_id]

    def get_sales(self, after_date):
        self._enter("get_sales")
        return list(self.sales)

    def create_offer_code(self, product_id: str, *, name: str, **_ignored):
        self._enter("create_offer_code")
        if product_id not in self.products:
            raise NotFound(product_id)
        self.writes.append(("offer_code", product_id, name))
        return {"name": name}
