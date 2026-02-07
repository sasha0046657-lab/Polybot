import time
import math
import requests
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"  # public endpoints are accessible w/o auth in docs

@dataclass
class MarketPick:
    market_id: str
    question: str
    token_id_yes: Optional[str]
    token_id_no: Optional[str]

@dataclass
class BookTop:
    bid_p: Optional[float] = None
    bid_s: Optional[float] = None
    ask_p: Optional[float] = None
    ask_s: Optional[float] = None

@dataclass
class PaperState:
    cash: float = 100.0
    pos: Dict[str, float] = field(default_factory=dict)  # token_id -> shares
    avg: Dict[str, float] = field(default_factory=dict)  # token_id -> avg entry price
    fees_paid: float = 0.0

def gamma_markets(limit=200) -> List[dict]:
    r = requests.get(f"{GAMMA_BASE}/markets", params={"closed":"false","limit":limit}, timeout=30)
    r.raise_for_status()
    return r.json()

def search_markets(query: str, max_hits=10) -> List[MarketPick]:
    q = query.lower().strip()
    hits = []
    for m in gamma_markets():
        question = (m.get("question") or "")
        if q in question.lower():
            ids = m.get("clobTokenIds") or []
            # обычно [YES, NO], но не всегда гарантировано
            token_yes = ids[0] if len(ids) > 0 else None
            token_no  = ids[1] if len(ids) > 1 else None
            hits.append(MarketPick(
                market_id=str(m.get("id")),
                question=question,
                token_id_yes=token_yes,
                token_id_no=token_no
            ))
        if len(hits) >= max_hits:
            break
    return hits

def get_orderbook(token_id: str) -> dict:
    # public endpoint used by official clients under the hood;
    # format: {"bids":[{"price":"0.55","size":"12"}...], "asks":[...]}
    r = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=15)
    r.raise_for_status()
    return r.json()

def top_of_book(ob: dict) -> BookTop:
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    top = BookTop()
    if bids:
        top.bid_p = float(bids[0]["price"]); top.bid_s = float(bids[0]["size"])
    if asks:
        top.ask_p = float(asks[0]["price"]); top.ask_s = float(asks[0]["size"])
    return top

def mid_spread(top: BookTop) -> Tuple[Optional[float], Optional[float]]:
    if top.bid_p is None or top.ask_p is None:
        return None, None
    return (top.bid_p + top.ask_p) / 2.0, (top.ask_p - top.bid_p)

def score_candidate(top: BookTop) -> float:
    """
    Простая “оценка интересности”:
    - хотим нормальный спред (чтобы был “край”)
    - хотим нормальную видимую ликвидность на top-of-book
    """
    mid, spr = mid_spread(top)
    if mid is None or spr is None:
        return -1e9
    liq = 0.0
    if top.bid_s: liq += math.log1p(top.bid_s)
    if top.ask_s: liq += math.log1p(top.ask_s)
    return (spr * 100.0) + liq  # спред в "центах" + лог ликвидности

def paper_fill(state: PaperState, token_id: str, side: str, px: float, size: float, fee_rate=0.0):
    """
    Paper fill: комиссия по умолчанию 0 (потому что комиссии/механика могут быть сложнее),
    при желании можно включить fee_rate.
    """
    cost = px * size
    fee = cost * fee_rate
    if side == "BUY":
        if state.cash < cost + fee:
            return False, "Not enough cash"
        state.cash -= (cost + fee)
        prev = state.pos.get(token_id, 0.0)
        prev_avg = state.avg.get(token_id, 0.0)
        new_pos = prev + size
        new_avg = (prev * prev_avg + size * px) / new_pos if new_pos > 0 else 0.0
        state.pos[token_id] = new_pos
        state.avg[token_id] = new_avg
        state.fees_paid += fee
        return True, f"BUY {size} @ {px:.4f}"
    else:
        prev = state.pos.get(token_id, 0.0)
        if prev < size:
            return False, "Not enough position"
        state.pos[token_id] = prev - size
        state.cash += (cost - fee)
        state.fees_paid += fee
        return True, f"SELL {size} @ {px:.4f}"

def mark_to_market(state: PaperState, prices: Dict[str, float]) -> float:
    nav = state.cash
    for tid, sh in state.pos.items():
        mid = prices.get(tid)
        if mid is not None:
            nav += sh * mid
    return nav

def main():
    print("=== Polymarket paper-bot (analysis + suggestions) ===")
    query = input("Введи ключевые слова рынка (например: 'election', 'bitcoin', 'Trump'): ").strip()
    hits = search_markets(query, max_hits=8)
    if not hits:
        print("Ничего не нашёл. Попробуй другое слово.")
        return

    print("\nНашёл рынки:")
    for i, h in enumerate(hits, 1):
        print(f"{i}) {h.question}")
        print(f"   market_id={h.market_id} YES={h.token_id_yes} NO={h.token_id_no}")

    idx = int(input("\nВыбери номер рынка: ").strip()) - 1
    pick = hits[idx]
    token = pick.token_id_yes or pick.token_id_no
    if not token:
        print("У этого рынка нет token_id в ответе Gamma API.")
        return

    side_token = "YES" if token == pick.token_id_yes else "NO"
    print(f"\nОк. Работаю с {side_token} token_id={token}")
    state = PaperState(cash=100.0)
    last_mids: List[float] = []

    while True:
        try:
            ob = get_orderbook(token)
            top = top_of_book(ob)
            mid, spr = mid_spread(top)
            if mid is None:
                print("Нет нормального bid/ask. Жду…")
                time.sleep(2)
                continue

            last_mids.append(mid)
            if len(last_mids) > 30:
                last_mids.pop(0)

            # “сигнал”: простая динамика
            mom = 0.0
            if len(last_mids) >= 10:
                mom = last_mids[-1] - last_mids[-10]  # рост/падение за ~10 тиков

            score = score_candidate(top)

            # Печать состояния
            prices = {token: mid}
            nav = mark_to_market(state, prices)
            pos = state.pos.get(token, 0.0)
            avg = state.avg.get(token, 0.0)
            print(f"\nmid={mid:.4f} spr={spr:.4f} topBid={top.bid_p}({top.bid_s}) topAsk={top.ask_p}({top.ask_s}) score={score:.2f}")
            print(f"paper: cash={state.cash:.2f} pos={pos:.2f} avg={avg:.4f} NAV={nav:.2f}")

            # Рекомендация (очень простая, только как “подсказка”)
            rec = None
            if spr is not None and spr >= 0.01:
                if mom > 0.01:
                    rec = "BUY small (momentum up)"
                elif mom < -0.01 and pos > 0:
                    rec = "SELL small (momentum down)"
                else:
                    rec = "HOLD / watch"
            else:
                rec = "HOLD (spread too small)"

            print("suggestion:", rec)

            # Интерактивный paper-trade
            cmd = input("Команда: [b]uy / [s]ell / [enter]=пропустить / [q]=выход: ").strip().lower()
            if cmd == "q":
                break
            if cmd in ("b","s"):
                size = float(input("Размер (shares), например 2: ").strip())
                # покупаем по ask, продаём по bid (как “маркет” в симуляции)
                if cmd == "b":
                    px = float(top.ask_p) if top.ask_p is not None else mid
                    ok, msg = paper_fill(state, token, "BUY", px, size)
                else:
                    px = float(top.bid_p) if top.bid_p is not None else mid
                    ok, msg = paper_fill(state, token, "SELL", px, size)
                print(("OK: " if ok else "NO: ") + msg)

            time.sleep(1)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print("ERROR:", e)
            time.sleep(2)

    print("\nDone.")

if __name__ == "__main__":
    main()
