import json, os, re, smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from io import StringIO
import pandas as pd, requests, yfinance as yf
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent
C=json.loads((ROOT/"config.json").read_text()); DATA=ROOT/"data.json"
H={"User-Agent":"Mozilla/5.0 Chrome/151 Safari/537.36"}
CF="https://stockanalysis.com/quote/asx/LOV/financials/cash-flow-statement/"
BS="https://stockanalysis.com/quote/asx/LOV/financials/balance-sheet/"
RAT="https://stockanalysis.com/quote/asx/LOV/financials/ratios/"
GF="https://www.gurufocus.com/term/pe-ratio/ASX%3ALOV"

def tables(url):
 r=requests.get(url,headers=H,timeout=30); r.raise_for_status()
 return pd.read_html(StringIO(r.text))

def yr(x):
 m=re.search(r"(20\d{2})",str(x)); return m.group(1) if m else None

def row(tabs,names):
 for d in tabs:
  if d.empty: continue
  first=d.columns[0]; labels=d[first].astype(str).str.strip().str.lower()
  for name in names:
   hit=d[labels==name.lower()]
   if not hit.empty:
    z={}
    for col in d.columns[1:]:
     y=yr(col)
     if not y: continue
     txt=str(hit.iloc[0][col]).replace(",","").replace("%","").strip()
     try: z[y]=float(txt)
     except: pass
    if z:return z
 return {}

def fundamentals():
 f=row(tables(CF),["Free Cash Flow"])
 sh=row(tables(BS),["Total Common Shares Outstanding","Filing Date Shares Outstanding"])
 years=sorted(set(f)&set(sh),key=int,reverse=True)[:C["fcf_years"]]
 if len(years)<C["fcf_years"]: raise RuntimeError(f"Only {len(years)} annual FCF/share periods found; need 7.")
 rows=[]
 for y in sorted(years,key=int):
  rows.append({"year":y,"fcf_aud_m":f[y],"shares_m":sh[y],"fcf_per_share_aud":f[y]/sh[y]})
 return sum(x["fcf_per_share_aud"] for x in rows)/7,rows

def sustainable_growth():
 tabs=tables(RAT); roe=payout=None
 for d in tabs:
  if d.empty:continue
  first=d.columns[0]; labels=d[first].astype(str).str.strip().str.lower()
  current=d.columns[1] if len(d.columns)>1 else None
  if current is None:continue
  for label,val in zip(labels,d[current]):
   try:num=float(str(val).replace(",","").replace("%","").strip())/100
   except:continue
   if label in {"return on equity (roe)","return on equity"}:roe=num
   elif label=="payout ratio":payout=num
 if roe is None or payout is None:raise RuntimeError("Could not obtain current ROE/payout ratio.")
 retention=1-payout
 return roe,payout,retention,roe*retention

def price():
 info=yf.Ticker("LOV.AX").info
 p=info.get("currentPrice") or info.get("regularMarketPrice")
 if p is None:raise RuntimeError("Yahoo did not return LOV price.")
 return float(p)

def pe():
 r=requests.get(GF,headers=H,timeout=30);r.raise_for_status()
 text=BeautifulSoup(r.text,"html.parser").get_text(" ",strip=True)
 m=re.search(r"10-year median(?:\s+of)?\s*([0-9]+(?:\.[0-9]+)?)",text,re.I)
 if not m:raise RuntimeError("GuruFocus 10-year median P/E not found for LOV.")
 return float(m.group(1))

def value(start,g,p):
 rr=.15;n=10;pv=0
 for y in range(1,n+1):
  cf=start*((1+g)**y);pv+=cf/((1+rr)**y)
 y10=start*((1+g)**n)
 return pv+(y10*p)/((1+rr)**n)

def email(d):
 u=os.getenv("SMTP_USERNAME","").strip();pw=os.getenv("SMTP_APP_PASSWORD","").strip()
 if not u or not pw:raise RuntimeError("SMTP secrets missing.")
 m=EmailMessage();m["From"]=u;m["To"]=C["alert_email"]
 m["Subject"]=f"LOV undervalued alert — A${d['price_aud']:.2f} vs A${d['intrinsic_value_aud']:.2f}"
 m.set_content(f"""Lovisa (ASX: LOV) has crossed below calculated intrinsic value.
Price: A${d['price_aud']:.2f}
Intrinsic value: A${d['intrinsic_value_aud']:.2f}
7Y avg FCF/share: A${d['avg_fcf_per_share_aud']:.2f}
Sustainable growth: {d['sustainable_growth_rate']*100:.2f}%
ROE: {d['roe']*100:.2f}%
Payout ratio: {d['payout_ratio']*100:.2f}%
Required return: 15%
10Y median P/E: {d['median_pe']:.2f}x""")
 with smtplib.SMTP("smtp.gmail.com",587,timeout=30) as s:
  s.starttls();s.login(u,pw);s.send_message(m)

def main():
 old=json.loads(DATA.read_text()) if DATA.exists() else {}
 avg,hist=fundamentals();roe,payout,retention,g=sustainable_growth()
 pr=price();multiple=pe();iv=value(avg,g,multiple);under=pr<iv
 d={"status":"ok","price_aud":pr,"intrinsic_value_aud":iv,
 "discount_to_value_pct":(iv-pr)/iv*100,"avg_fcf_per_share_aud":avg,
 "growth_rate":g,"sustainable_growth_rate":g,"roe":roe,"payout_ratio":payout,
 "retention_ratio":retention,"median_pe":multiple,"undervalued":under,
 "updated_at":datetime.now(timezone.utc).isoformat(),"fcf_history":hist,
 "sources":{"fundamentals":"StockAnalysis/S&P Global annual LOV financials",
 "growth":"Current ROE and payout ratio; SGR = ROE × (1 − payout ratio)",
 "yahoo":"Live Yahoo Finance LOV price","gurufocus":"Live GuruFocus 10Y median P/E"}}
 DATA.write_text(json.dumps(d,indent=2))
 if under and not bool(old.get("undervalued",False)):email(d)
if __name__=="__main__":main()
