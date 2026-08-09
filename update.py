import json, os, re, smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from io import StringIO
import pandas as pd, requests, yfinance as yf
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent
C=json.loads((ROOT/"config.json").read_text())
DATA=ROOT/"data.json"
HEADERS={"User-Agent":"Mozilla/5.0 Chrome/151 Safari/537.36"}
CF="https://stockanalysis.com/quote/asx/LOV/financials/cash-flow-statement/"
BS="https://stockanalysis.com/quote/asx/LOV/financials/balance-sheet/"
RAT="https://stockanalysis.com/quote/asx/LOV/financials/ratios/"
GF="https://www.gurufocus.com/term/pe-ratio/ASX%3ALOV"

def get_tables(url):
    r=requests.get(url,headers=HEADERS,timeout=30)
    r.raise_for_status()
    return pd.read_html(StringIO(r.text))

def year_from_col(x):
    m=re.search(r"(20\d{2})",str(x))
    return m.group(1) if m else None

def extract_row(tables,names):
    for df in tables:
        if df.empty: continue
        first=df.columns[0]
        labels=df[first].astype(str).str.strip().str.lower()
        for name in names:
            hit=df[labels==name.lower()]
            if hit.empty: continue
            row=hit.iloc[0]
            out={}
            for col in df.columns[1:]:
                y=year_from_col(col)
                if not y: continue
                txt=str(row[col]).replace(",","").replace("%","").strip()
                try: out[y]=float(txt)
                except: pass
            if out: return out
    return {}

def fundamentals():
    history=json.loads((ROOT/"history_seed.json").read_text())
    fcf=extract_row(get_tables(CF),["Free Cash Flow"])
    shares=extract_row(get_tables(BS),[
        "Total Common Shares Outstanding",
        "Filing Date Shares Outstanding"
    ])
    refreshed=0
    for y in sorted(set(fcf)&set(shares)):
        if fcf[y]>0 and shares[y]>0:
            history[y]={"fcf_aud_m":float(fcf[y]),"shares_m":float(shares[y])}
            refreshed+=1
    years=sorted(history.keys(),key=int,reverse=True)[:C["fcf_years"]]
    if len(years)<C["fcf_years"]:
        raise RuntimeError(f"Only {len(years)} annual periods available; need 7.")
    rows=[]
    for y in sorted(years,key=int):
        f=float(history[y]["fcf_aud_m"]); sh=float(history[y]["shares_m"])
        rows.append({"year":y,"fcf_aud_m":f,"shares_m":sh,"fcf_per_share_aud":f/sh})
    avg=sum(x["fcf_per_share_aud"] for x in rows)/len(rows)
    return avg,rows,refreshed

def sustainable_growth():
    tabs=get_tables(RAT)
    roe=payout=None
    for df in tabs:
        if df.empty or len(df.columns)<2: continue
        first=df.columns[0]
        labels=df[first].astype(str).str.strip().str.lower()
        current=df.columns[1]
        for label,val in zip(labels,df[current]):
            txt=str(val).replace(",","").replace("%","").strip()
            try: num=float(txt)/100.0
            except: continue
            if label in {"return on equity (roe)","return on equity"}: roe=num
            elif label=="payout ratio": payout=num
    if roe is None or payout is None:
        raise RuntimeError("Could not obtain current ROE and payout ratio.")
    retention=1-payout
    return roe,payout,retention,roe*retention

def current_price():
    info=yf.Ticker(C["ticker"]).info
    p=info.get("currentPrice") or info.get("regularMarketPrice")
    if p is None: raise RuntimeError("Yahoo did not return LOV price.")
    return float(p)

def median_pe():
    try:
        r=requests.get(GF,headers=HEADERS,timeout=30)
        r.raise_for_status()
        text=BeautifulSoup(r.text,"html.parser").get_text(" ",strip=True)
        m=re.search(r"10-year median(?:\s+of)?\s*([0-9]+(?:\.[0-9]+)?)",text,re.I)
        if not m: raise RuntimeError("median not found")
        return float(m.group(1)),"Live GuruFocus"
    except Exception as e:
        return float(C["fallback_median_pe"]),f"Fallback 36.97x; live fetch failed: {e}"

def intrinsic(start,g,pe):
    rr=C["required_return"]; n=C["forecast_years"]; pv=0.0
    for y in range(1,n+1):
        cf=start*((1+g)**y)
        pv += cf/((1+rr)**y)
    y10=start*((1+g)**n)
    pv += (y10*pe)/((1+rr)**n)
    return pv

def send_email(d):
    u=os.getenv("SMTP_USERNAME","").strip()
    pw=os.getenv("SMTP_APP_PASSWORD","").strip()
    if not u or not pw: raise RuntimeError("SMTP secrets missing.")
    m=EmailMessage()
    m["From"]=u
    m["To"]=C["alert_email"]
    m["Subject"]=f"LOV undervalued alert — A${d['price_aud']:.2f} vs A${d['intrinsic_value_aud']:.2f}"
    m.set_content(f"""Lovisa (ASX: LOV) has crossed below calculated intrinsic value.

Price: A${d['price_aud']:.2f}
Intrinsic value: A${d['intrinsic_value_aud']:.2f}
7Y avg FCF/share: A${d['avg_fcf_per_share_aud']:.2f}
Sustainable growth: {d['sustainable_growth_rate']*100:.2f}%
ROE: {d['roe']*100:.2f}%
Payout ratio: {d['payout_ratio']*100:.2f}%
Required return: 15%
10Y median P/E: {d['median_pe']:.2f}x
""")
    with smtplib.SMTP("smtp.gmail.com",587,timeout=30) as s:
        s.starttls(); s.login(u,pw); s.send_message(m)

def main():
    old=json.loads(DATA.read_text()) if DATA.exists() else {}
    avg,hist,refreshed=fundamentals()
    roe,payout,retention,g=sustainable_growth()
    pr=current_price()
    pe,gf_status=median_pe()
    iv=intrinsic(avg,g,pe)
    under=pr<iv
    d={
      "status":"ok","price_aud":pr,"intrinsic_value_aud":iv,
      "discount_to_value_pct":(iv-pr)/iv*100,
      "avg_fcf_per_share_aud":avg,
      "sustainable_growth_rate":g,"roe":roe,"payout_ratio":payout,
      "retention_ratio":retention,"median_pe":pe,"undervalued":under,
      "updated_at":datetime.now(timezone.utc).isoformat(),
      "fcf_history":hist,
      "sources":{
        "fundamentals":f"7-year rolling history; {refreshed} recent years refreshed from StockAnalysis/S&P Global; FY2019/FY2020 seeded from Lovisa annual reports",
        "growth":"Current ROE and payout ratio; SGR = ROE × (1 − payout ratio)",
        "yahoo":"Live Yahoo Finance LOV price",
        "gurufocus":gf_status
      }
    }
    DATA.write_text(json.dumps(d,indent=2))
    if under and not bool(old.get("undervalued",False)):
        send_email(d)

if __name__=="__main__":
    main()
