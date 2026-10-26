"""Price validation checks. WooCommerce is mocked; no HTTP requests are sent."""
import asyncio
import importlib.util
import math
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.update(WC_KEY="fixture-key", WC_SECRET="fixture-secret")
spec = importlib.util.spec_from_file_location("woo_price_fixture", Path(__file__).resolve().parents[1] / "main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


async def check():
    product = {"id": 1, "name": "Tea", "status": "publish"}
    upstream = AsyncMock(return_value=product)
    with patch.object(module, "_call", upstream):
        for invalid in [math.nan, math.inf, -math.inf, -1.0]:
            for execute in [
                lambda: module.create_product("Tea", invalid),
                lambda: module.update_product(1, regular_price=invalid),
                lambda: module.update_product(1, sale_price=invalid),
            ]:
                before = upstream.await_count
                try:
                    await execute()
                except ValueError:
                    pass
                else:
                    raise AssertionError("An invalid price was accepted")
                assert upstream.await_count == before, "Invalid prices must send no request"
        for value, expected in [(0.0, "0.00"), (299.0, "299.00"), (12.5, "12.50")]:
            await module.create_product("Tea", value)
            assert upstream.call_args.kwargs["json"]["regular_price"] == expected
            await module.update_product(1, regular_price=value)
            assert upstream.call_args.kwargs["json"]["regular_price"] == expected
            await module.update_product(1, sale_price=value)
            assert upstream.call_args.kwargs["json"]["sale_price"] == ("" if value == 0 else expected)
    print("Finite non-negative prices keep two decimals; zero sale price still removes the sale.")
    print("Negative, NaN and infinite prices stop before any WooCommerce request.")
    print("Product price verification passed. All upstream calls were mocked.")


if __name__ == "__main__":
    asyncio.run(check())
