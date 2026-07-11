import pandas as pd


def _body(o: float, c: float) -> float:
    return abs(c - o)


def _upper_wick(o: float, h: float, c: float) -> float:
    return h - max(o, c)


def _lower_wick(o: float, l: float, c: float) -> float:
    return min(o, c) - l


def detect_hammer(df: pd.DataFrame) -> bool:
    row = df.iloc[-1]
    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
    body = _body(o, c)
    lower_wick = _lower_wick(o, l, c)
    upper_wick = _upper_wick(o, h, c)
    if body == 0:
        return False
    return lower_wick >= 2 * body and upper_wick <= 0.3 * body


def detect_shooting_star(df: pd.DataFrame) -> bool:
    row = df.iloc[-1]
    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
    body = _body(o, c)
    upper_wick = _upper_wick(o, h, c)
    lower_wick = _lower_wick(o, l, c)
    if body == 0:
        return False
    return upper_wick >= 2 * body and lower_wick <= 0.3 * body


def detect_bullish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    prev_bearish = float(prev["Close"]) < float(prev["Open"])
    curr_bullish = float(curr["Close"]) > float(curr["Open"])
    engulfs = float(curr["Open"]) <= float(prev["Close"]) and float(curr["Close"]) >= float(prev["Open"])
    return prev_bearish and curr_bullish and engulfs


def detect_bearish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    prev_bullish = float(prev["Close"]) > float(prev["Open"])
    curr_bearish = float(curr["Close"]) < float(curr["Open"])
    engulfs = float(curr["Open"]) >= float(prev["Close"]) and float(curr["Close"]) <= float(prev["Open"])
    return prev_bullish and curr_bearish and engulfs


def detect_doji(df: pd.DataFrame, threshold: float = 0.001) -> bool:
    row = df.iloc[-1]
    o, c = float(row["Open"]), float(row["Close"])
    price_range = float(row["High"]) - float(row["Low"])
    if price_range == 0:
        return False
    return abs(c - o) / price_range < threshold
