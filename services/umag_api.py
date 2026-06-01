"""
HTTP-клиент для REST API Umag (api.umag.kz).

Полный flow создания приёмки:
  login → create_supply → edit (set supplier) → add_products → provide
"""

import logging
from base64 import b64encode
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://api.umag.kz"

_COMMON_HEADERS = {
    "client-ver": "angular_cabinet_17.17.7",
    "api-ver": "1.4",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}


class UmagAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"Umag API {status}: {message}")


class UmagAPI:
    def __init__(self, store_id: int, login: str, password: str):
        self.store_id = store_id
        self._login = login
        self._password = password
        self._token: Optional[str] = None

    async def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: Optional[Dict] = None,
        auth_header: Optional[str] = None,
    ) -> Any:
        headers = dict(_COMMON_HEADERS)
        if auth_header:
            headers["Authorization"] = auth_header
        elif self._token:
            headers["Authorization"] = self._token

        if params is None:
            params = {}
        params.setdefault("storeId", self.store_id)

        url = BASE_URL + path

        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url, json=body, params=params, headers=headers,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise UmagAPIError(resp.status, text[:300])
                if not text:
                    return None
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    return text

    # ── Auth ─────────────────────────────────────────────────────────────────

    async def login(self) -> str:
        creds = b64encode(f"{self._login}:{self._password}".encode()).decode()
        data = await self._request(
            "GET",
            "/rest/cabinet/org/login/signin",
            auth_header=f"Basic {creds}",
            params={"storeId": self.store_id},
        )
        if isinstance(data, dict):
            self._token = (
                data.get("sessionToken")
                or data.get("accessToken")
                or data.get("token")
            )
        elif isinstance(data, str):
            self._token = data.strip().strip('"')
        if not self._token:
            raise UmagAPIError(0, "Не удалось получить токен из ответа")
        logger.info("Umag: авторизация успешна, userId=%s", data.get("userId") if isinstance(data, dict) else "?")
        return self._token

    async def ensure_auth(self):
        if not self._token:
            await self.login()

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def get_accounts(self) -> List[Dict]:
        await self.ensure_auth()
        data = await self._request("GET", "/rest/cabinet/fin/account/list")
        if isinstance(data, dict):
            return data.get("accounts", [])
        return []

    # ── Suppliers ────────────────────────────────────────────────────────────

    async def get_suppliers(self) -> List[Dict]:
        await self.ensure_auth()
        data = await self._request("GET", "/rest/cabinet/org/agent/list-agent-names")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("agents", data.get("items", []))
        return []

    # ── Supply (Приёмка) ─────────────────────────────────────────────────────

    async def create_supply(self) -> Dict:
        await self.ensure_auth()
        data = await self._request(
            "POST", "/rest/cabinet/opr/supplies/v2/create", body={},
        )
        logger.info("Umag: приёмка создана, id=%s", data)
        return data

    async def edit_supply(
        self,
        supply_id: int,
        supplier_id: Optional[int] = None,
        comment: Optional[str] = None,
    ) -> Any:
        await self.ensure_auth()
        body = {
            "comment": comment,
            "docTime": None,
            "supplierId": supplier_id,
        }
        return await self._request(
            "POST",
            f"/rest/cabinet/opr/supplies/v2/{supply_id}/edit",
            body=body,
        )

    async def add_products(
        self,
        supply_id: int,
        products: List[Dict],
    ) -> Any:
        await self.ensure_auth()
        return await self._request(
            "POST",
            f"/rest/cabinet/opr/supplies/v2/{supply_id}/add-products",
            body={"products": products},
        )

    async def provide_supply(
        self,
        supply_id: int,
        account_id: int,
        amount: float,
    ) -> Any:
        await self.ensure_auth()
        body = {
            "payment": {
                "accountId": account_id,
                "amount": amount,
                "paymentComment": "",
                "paymentType": "FULL_SUPPLY",
            },
            "settings": {
                "isBindToSupplier": False,
            },
        }
        return await self._request(
            "POST",
            f"/rest/cabinet/opr/supplies/v2/{supply_id}/provide",
            body=body,
        )

    async def delete_supply(self, supply_id: int) -> Any:
        await self.ensure_auth()
        return await self._request(
            "POST",
            f"/rest/cabinet/opr/supplies/v2/{supply_id}/delete",
        )

    # ── Высокоуровневый метод ────────────────────────────────────────────────

    async def upload_invoice(
        self,
        items: List[Dict],
        supplier_id: Optional[int] = None,
        account_id: Optional[int] = None,
        auto_provide: bool = False,
    ) -> Dict:
        """
        Создать приёмку и загрузить товары.

        items: список позиций из invoice_items (после _finalize).
        Каждый элемент должен содержать: product_barcode, quantity, price, selling_price.

        Возвращает {"supply_id": ..., "provided": bool, "products_count": int}.
        """
        await self.ensure_auth()

        # 1. Создать приёмку
        supply_data = await self.create_supply()
        supply_id = self._extract_supply_id(supply_data)

        # 2. Задать поставщика (если указан)
        if supplier_id:
            await self.edit_supply(supply_id, supplier_id=supplier_id)

        # 3. Подготовить товары
        products = []
        total_amount = 0.0
        for item in items:
            if item.get("match_type") == "skipped":
                continue
            barcode = item.get("product_barcode") or item.get("supplier_barcode")
            if not barcode:
                continue

            qty = item.get("quantity") or 1
            price = item.get("price") or 0
            selling_price = item.get("selling_price") or 0

            try:
                barcode_num = int(barcode)
            except (ValueError, TypeError):
                barcode_num = barcode

            products.append({
                "barcode": barcode_num,
                "quantity": qty,
                "discount": 0,
                "price": price,
                "sellingPrice": selling_price,
                "arrivalCost": price,
                "type": "PRODUCT",
            })
            total_amount += qty * price

        if not products:
            await self.delete_supply(supply_id)
            raise UmagAPIError(0, "Нет товаров с штрихкодами для загрузки")

        # 4. Добавить товары
        await self.add_products(supply_id, products)
        logger.info("Umag: добавлено %d товаров в приёмку %d", len(products), supply_id)

        # 5. Провести (если нужно)
        provided = False
        if auto_provide and account_id and supplier_id:
            try:
                await self.provide_supply(supply_id, account_id, total_amount)
                provided = True
                logger.info("Umag: приёмка %d проведена", supply_id)
            except UmagAPIError as e:
                logger.warning("Umag: не удалось провести приёмку %d: %s", supply_id, e)

        return {
            "supply_id": supply_id,
            "provided": provided,
            "products_count": len(products),
            "total_amount": total_amount,
        }

    @staticmethod
    def _extract_supply_id(data: Any) -> int:
        if isinstance(data, dict):
            return data.get("id") or data.get("supplyId")
        if isinstance(data, (int, float)):
            return int(data)
        if isinstance(data, str):
            return int(data.strip().strip('"'))
        raise UmagAPIError(0, f"Не удалось извлечь ID приёмки из: {data!r}")
