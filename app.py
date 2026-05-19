from flask import Flask, jsonify
from flask_cors import CORS
import requests
import hmac
import hashlib
from datetime import datetime, timedelta
import time

app = Flask(__name__)
CORS(app)

COUPANG_VENDOR_ID = "A01277851"
COUPANG_ACCESS_KEY = "0501bd84-c28a-43f1-9576-ef6d83914f63"
COUPANG_SECRET_KEY = "e55693c99b7c271efb1e3144aec3a93b7875dbf8"

def make_signature(method, path, query):
    now = datetime.utcnow()
    datetime_str = now.strftime("%y%m%dT%H%M%SZ")
    message = datetime_str + method + path + query
    signature = hmac.new(
        COUPANG_SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    auth = (f"CEA algorithm=HmacSHA256, access-key={COUPANG_ACCESS_KEY}, "
            f"signed-date={datetime_str}, signature={signature}")
    return auth

def parse_order_date(raw):
    import re
    if not raw:
        return ""
    raw = str(raw).strip()
    m = re.search(r'(\d{4})년\s*(\d+)월\s*(\d+)일', raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if len(raw) >= 10 and raw[4] == '-':
        return raw[:10]
    return raw[:10]

def accept_to_instruct(ship_box_ids):
    if not ship_box_ids:
        return 0
    try:
        converted = 0
        for i in range(0, len(ship_box_ids), 50):
            chunk = ship_box_ids[i:i+50]
            path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/ordersheets/acknowledgement"
            import json
            body = json.dumps({"vendorId": COUPANG_VENDOR_ID, "shipmentBoxIds": chunk})
            now = datetime.utcnow()
            datetime_str = now.strftime("%y%m%dT%H%M%SZ")
            message = datetime_str + "PUT" + path + ""
            signature = hmac.new(
                COUPANG_SECRET_KEY.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            auth = (f"CEA algorithm=HmacSHA256, access-key={COUPANG_ACCESS_KEY}, "
                    f"signed-date={datetime_str}, signature={signature}")
            resp = requests.put(
                f"https://api-gateway.coupang.com{path}",
                headers={"Authorization": auth, "Content-Type": "application/json"},
                data=body, timeout=10
            )
            result = resp.json()
            if str(result.get("code")) == "200":
                converted += len(chunk)
        return converted
    except Exception as e:
        print(f"[전환 오류] {e}")
        return 0

@app.route("/")
def index():
    return jsonify({"status": "ok", "message": "모드팜 API 서버"})

@app.route("/orders")
def get_coupang_orders():
    try:
        path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/ordersheets"
        now = datetime.utcnow()
        created_from = (now - timedelta(days=14)).strftime("%Y-%m-%d")
        created_to = now.strftime("%Y-%m-%d")
        all_orders = []
        seen_ids = set()

        # 결제완료 → 상품준비중 전환
        accept_query = f"createdAtFrom={created_from}&createdAtTo={created_to}&status=ACCEPT&maxPerPage=50"
        auth = make_signature("GET", path, accept_query)
        resp = requests.get(f"https://api-gateway.coupang.com{path}?{accept_query}",
                            headers={"Authorization": auth, "Content-Type": "application/json"}, timeout=10)
        accept_result = resp.json()
        if str(accept_result.get("code")) == "200":
            accept_data = accept_result.get("data", [])
            accept_sheets = accept_data if isinstance(accept_data, list) else []
            ship_box_ids = [sheet.get("shipmentBoxId") for sheet in accept_sheets if sheet.get("shipmentBoxId")]
            if ship_box_ids:
                accept_to_instruct(ship_box_ids)
                time.sleep(2)

        # 상품준비중 수집
        next_token = ""
        page = 0
        while True:
            page += 1
            if next_token:
                query = f"createdAtFrom={created_from}&createdAtTo={created_to}&status=INSTRUCT&maxPerPage=50&nextToken={next_token}"
            else:
                query = f"createdAtFrom={created_from}&createdAtTo={created_to}&status=INSTRUCT&maxPerPage=50"

            auth = make_signature("GET", path, query)
            url = f"https://api-gateway.coupang.com{path}?{query}"
            resp = requests.get(url, headers={"Authorization": auth, "Content-Type": "application/json"}, timeout=10)
            result = resp.json()

            if str(result.get("code")) != "200":
                break

            data = result.get("data", [])
            sheet_list = data if isinstance(data, list) else data.get("orderSheets", [])

            # 디버그: 각 sheet의 orderId와 orderItems 개수 출력
            for s in sheet_list[:5]:
                print(f"[DEBUG] orderId={s.get('orderId')} items={len(s.get('orderItems',[]))} shipmentBoxId={s.get('shipmentBoxId')}")

            for sheet in sheet_list:
                receiver = sheet.get("receiver", {})
                order_id = str(sheet.get("orderId", ""))
                is_cancel = sheet.get("status", "") in ("CANCEL_REQUEST", "CANCELED")

                shipment_box_id = str(sheet.get("shipmentBoxId", ""))

                for item in sheet.get("orderItems", []):
                    item_id = str(item.get("orderItemId", ""))
                    # shipmentBoxId로 고유 식별 (같은 orderId라도 상품별로 다른 shipmentBoxId)
                    oid = f"{order_id}-{shipment_box_id}" if shipment_box_id else f"{order_id}-{item_id}"

                    if oid in seen_ids:
                        continue
                    seen_ids.add(oid)

                    product = (item.get("sellerProductName", "") or
                               item.get("productName", "") or
                               item.get("vendorItemPackageName", "") or "")
                    option_str = (item.get("sellerProductItemName", "") or
                                  item.get("vendorItemName", "") or "")
                    if option_str and option_str == product:
                        option_str = ""

                    addr1 = receiver.get("addr1", "")
                    addr2 = receiver.get("addr2", "")
                    zipcode = receiver.get("postCode", "") or receiver.get("zipCode", "")
                    price = item.get("salesPrice", 0) or item.get("orderPrice", 0)
                    qty = item.get("shippingCount", 0) or item.get("quantity", 1)
                    remote_price = sheet.get("remotePrice", 0) or 0
                    remote_area = sheet.get("remoteArea", False)

                    all_orders.append({
                        "order_id": oid,
                        "mall": "쿠팡",
                        "order_date": parse_order_date(sheet.get("orderedAt", "")),
                        "ordered_at": sheet.get("orderedAt", "") or "",
                        "product": product,
                        "option": option_str,
                        "qty": qty,
                        "buyer": receiver.get("name", ""),
                        "phone": receiver.get("safeNumber", "") or receiver.get("phone", ""),
                        "zipcode": zipcode,
                        "address": addr1,
                        "address2": addr2,
                        "delivery_msg": sheet.get("parcelPrintMessage", ""),
                        "price": price,
                        "cancel": is_cancel,
                        "remote_area": remote_area,
                        "remote_price": remote_price,
                    })

            next_token = result.get("nextToken", "")
            if not next_token or not sheet_list:
                break
            if page >= 20:
                break

        return jsonify({"success": True, "orders": all_orders, "count": len(all_orders)})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
