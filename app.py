from flask import Flask, jsonify
from flask_cors import CORS
import requests
import hmac
import hashlib
from datetime import datetime, timedelta
import time
import json

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
            print(f"[전환 응답] code={result.get('code')} message={result.get('message')}")
            if str(result.get("code")) == "200":
                converted += len(chunk)
        return converted
    except Exception as e:
        print(f"[전환 오류] {e}")
        return 0

@app.route("/")
def index():
    return jsonify({"status": "ok", "message": "모드팜 API 서버"})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/coupang/orders")
def get_coupang_orders():
    return _fetch_orders()

@app.route("/orders")
def get_orders():
    return _fetch_orders()

def _fetch_orders():
    try:
        path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/ordersheets"
        now = datetime.utcnow()
        created_from = (now - timedelta(days=14)).strftime("%Y-%m-%d")
        created_to = now.strftime("%Y-%m-%d")
        all_orders = []
        seen_ids = set()

        print(f"[수집 시작] 기간: {created_from} ~ {created_to}")

        # 결제완료(ACCEPT) → 상품준비중(INSTRUCT) 전환
        accept_query = f"createdAtFrom={created_from}&createdAtTo={created_to}&status=ACCEPT&maxPerPage=50"
        auth = make_signature("GET", path, accept_query)
        resp = requests.get(
            f"https://api-gateway.coupang.com{path}?{accept_query}",
            headers={"Authorization": auth, "Content-Type": "application/json"},
            timeout=10
        )
        accept_result = resp.json()
        print(f"[ACCEPT 조회] code={accept_result.get('code')} message={accept_result.get('message')}")

        if str(accept_result.get("code")) == "200":
            accept_data = accept_result.get("data", [])
            if isinstance(accept_data, dict):
                accept_sheets = accept_data.get("orderSheets", [])
            else:
                accept_sheets = accept_data if isinstance(accept_data, list) else []
            print(f"[ACCEPT 건수] {len(accept_sheets)}건")
            ship_box_ids = [sheet.get("shipmentBoxId") for sheet in accept_sheets if sheet.get("shipmentBoxId")]
            print(f"[전환 대상 shipmentBoxId] {ship_box_ids}")
            if ship_box_ids:
                converted = accept_to_instruct(ship_box_ids)
                print(f"[전환 완료] {converted}건")
                time.sleep(2)
        else:
            print(f"[ACCEPT 조회 실패] {accept_result}")

        # 상품준비중(INSTRUCT) 수집
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
            resp = requests.get(url, headers={"Authorization": auth, "Content-Type": "application/json"}, timeout=15)
            result = resp.json()
            print(f"[INSTRUCT 페이지{page}] code={result.get('code')} message={result.get('message')}")

            if str(result.get("code")) != "200":
                print(f"[INSTRUCT 실패] 전체 응답: {json.dumps(result, ensure_ascii=False)[:500]}")
                break

            data = result.get("data", [])
            print(f"[data 타입] {type(data).__name__} / 값 미리보기: {str(data)[:300]}")

            if isinstance(data, dict):
                sheet_list = data.get("orderSheets", [])
            elif isinstance(data, list):
                sheet_list = data
            else:
                sheet_list = []

            print(f"[INSTRUCT 페이지{page}] sheet_list 건수: {len(sheet_list)}")

            for sheet in sheet_list:
                receiver = sheet.get("receiver", {})
                order_id = str(sheet.get("orderId", ""))
                is_cancel = sheet.get("status", "") in ("CANCEL_REQUEST", "CANCELED")
                order_items = sheet.get("orderItems", [])
                print(f"  [주문] orderId={order_id} items={len(order_items)}개 status={sheet.get('status')}")

                for item in order_items:
                    item_id = str(item.get("orderItemId", ""))
                    oid = order_id if len(order_items) == 1 else f"{order_id}-{item_id}"
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

                    all_orders.append({
                        "order_id": oid,
                        "mall": "쿠팡",
                        "order_date": parse_order_date(sheet.get("orderedAt", "")),
                        "ordered_at": sheet.get("orderedAt", "") or "",
                        "product": product,
                        "option": option_str,
                        "qty": item.get("quantity", 1),
                        "buyer": receiver.get("name", ""),
                        "phone": receiver.get("safeNumber", "") or receiver.get("phone", ""),
                        "zipcode": zipcode,
                        "address": addr1,
                        "address2": addr2,
                        "delivery_msg": sheet.get("parcelPrintMessage", ""),
                        "price": price,
                        "cancel": is_cancel,
                    })

            next_token = result.get("nextToken", "")
            if not next_token or not sheet_list:
                print(f"[수집 완료] 총 {len(all_orders)}건")
                break
            if page >= 20:
                break

        return jsonify({"success": True, "orders": all_orders, "count": len(all_orders)})

    except Exception as e:
        import traceback
        print(f"[오류] {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
