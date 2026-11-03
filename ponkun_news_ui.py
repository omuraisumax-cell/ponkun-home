"""Standalone Ponkun News page; chat rendering and routes remain untouched."""

PAGE = r'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#101827"><title>Ponkun News</title>
<style>
:root{color-scheme:dark;--bg:#101827;--panel:#1b283a;--panel2:#223248;--text:#f0f5ff;--muted:#a5b6cb;--accent:#8d9cff;--line:#34445c}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Yu Gothic",sans-serif}
body{background:radial-gradient(ellipse at top right,#20254b 0,transparent 45%),var(--bg)}
.shell{width:min(100%,900px);margin:auto;padding:20px max(16px,env(safe-area-inset-right)) 70px max(16px,env(safe-area-inset-left))}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:24px}a{color:#b8c2ff}header a{text-decoration:none;font-size:14px}h1{font-size:clamp(25px,6vw,34px);margin:8px 0}.eyebrow{color:var(--accent);font-weight:700;letter-spacing:.08em}.intro{color:var(--muted);line-height:1.7;margin:0 0 20px}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:18px}button{border:1px solid var(--line);border-radius:13px;padding:10px 14px;min-height:44px;background:var(--panel2);color:var(--text);font:inherit;cursor:pointer}button:hover{border-color:var(--accent)}button:disabled{opacity:.5;cursor:not-allowed}.primary{background:linear-gradient(120deg,#5865de,#6979ef);border-color:transparent;font-weight:700}
.usage,.message{color:var(--muted);font-size:13px}.message{min-height:1.5em;margin:6px 0 16px}.message.error{color:#ffbdc1}
details{border:1px solid var(--line);border-radius:18px;padding:14px 16px;background:var(--panel);margin-bottom:24px}summary{cursor:pointer;font-weight:700}label{display:block;margin:14px 0 6px;font-size:14px;color:var(--muted)}textarea,input[type=number]{width:100%;border:1px solid var(--line);border-radius:12px;background:#111c2c;color:var(--text);padding:12px;font:inherit}textarea{min-height:125px;resize:vertical;line-height:1.6}small{color:var(--muted)}
.list{display:grid;gap:14px}.card{border:1px solid var(--line);border-radius:20px;background:var(--panel);padding:18px;box-shadow:0 8px 30px #03091430}.meta{display:flex;gap:8px;flex-wrap:wrap;color:var(--muted);font-size:12px}.tag{background:#343c6c;color:#dce0ff;padding:2px 8px;border-radius:30px}h2{font-size:18px;line-height:1.45;margin:12px 0}.card p{line-height:1.7;margin:8px 0}.reason{color:#c5d2e3;font-size:14px}.actions{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin-top:16px}.actions button{font-size:13px;padding:7px 10px;min-height:40px}.actions button[aria-pressed="true"]{border-color:var(--accent);background:#384683}.source{margin-left:auto;font-size:13px}.empty{border:1px dashed var(--line);padding:32px 18px;border-radius:18px;color:var(--muted);text-align:center;line-height:1.7}
@media(max-width:430px){.shell{padding-top:12px}.card{padding:15px}.source{margin-left:0;width:100%}.toolbar .usage{width:100%}}
</style></head><body><div class="shell">
<header><div class="eyebrow">PONKUN HOME</div><a href="/">← チャットへ戻る</a></header>
<h1>📰 Ponkun News</h1><p class="intro">あなたの関心に合わせた新着記事を、日本語の短い要約で。原文は出典リンクから読めます。</p>
<div class="toolbar"><button id="refresh" class="primary" type="button">ニュースを更新</button><span id="usage" class="usage"></span></div>
<small id="limitsNote">推定費用の上限により、回数上限より早く停止する場合があります。</small>
<small style="display:block;margin-top:4px">実際のAPI請求額と、アプリ内の推定額は異なる場合があります。</small>
<div id="message" class="message" role="status" aria-live="polite"></div>
<details><summary>興味のあるジャンルを編集</summary><label for="topics">1行に1ジャンル（追加・編集・削除できます）</label><textarea id="topics" maxlength="600"></textarea><small>最大12ジャンル、各40文字まで。保存だけでは検索は実行されません。</small><div style="margin-top:12px"><button id="saveTopics" type="button">ジャンルを保存</button></div></details>
<details><summary>ニュース設定・検索上限</summary>
<label for="dailyLimit">1日の検索回数上限（日本時間）</label><input id="dailyLimit" type="number" min="1" max="100" step="1">
<label for="monthlyLimit">1か月の検索回数上限（日本時間）</label><input id="monthlyLimit" type="number" min="1" max="1000" step="1">
<label for="monthlyUsd">月間の推定予算（USD）</label><input id="monthlyUsd" type="number" min="0.1" max="1000" step="0.01">
<p><small>上限の変更は次の検索から適用されます。保存しても検索は実行されません。</small></p>
<button id="saveLimits" type="button">上限を保存</button></details>
<main id="articles" class="list" aria-label="ニュース一覧"></main>
</div><script>
const $ = id => document.getElementById(id);
let busy = false;
function setMessage(text, error=false){$('message').textContent=text;$('message').classList.toggle('error',error)}
function csrf(){const item=document.cookie.split(';').map(x=>x.trim()).find(x=>x.startsWith('ponkun_mobile_csrf='));return item?decodeURIComponent(item.split('=').slice(1).join('=')):''}
async function api(path,options={}){const headers=new Headers(options.headers||{});if(options.method==='POST')headers.set('X-CSRF-Token',csrf());const res=await fetch(path,{...options,headers,credentials:'same-origin'});if(res.status===401){location.href='/login';throw Error('認証が必要です')}if(!res.ok){let detail='操作に失敗しました';try{detail=(await res.json()).detail||detail}catch{}throw Error(detail)}return res.json()}
function el(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node}
function render(data){$('topics').value=data.topics.join('\n');$('usage').textContent=`今月 ${data.usage.calls} / ${data.usage.monthly_call_limit} 回 · 推定 $${data.usage.estimated_usd.toFixed(3)} / $${data.usage.monthly_limit_usd.toFixed(2)}`;$('limitsNote').textContent=`1日${data.usage.daily_limit}回まで。推定費用の上限により、回数上限より早く停止する場合があります。`;const list=$('articles');list.replaceChildren();if(!data.articles.length){list.append(el('div','empty','まだ記事はありません。「ニュースを更新」を押すと新着記事を探します。'));return}for(const item of data.articles){const card=el('article','card');const meta=el('div','meta');meta.append(el('span','tag',item.topic||'ニュース'),el('time','',item.published_at));const title=el('h2','',item.title);const summary=el('p','',item.summary);const reason=el('p','reason','おすすめ理由：'+item.reason);const actions=el('div','actions');for(const [state,label] of [['interested','興味あり'],['not_interested','興味なし'],['read','既読']]){const button=el('button','',label);button.type='button';button.setAttribute('aria-pressed',String(item.state===state));button.onclick=async()=>{button.disabled=true;try{await api('/news/api/articles/'+encodeURIComponent(item.id)+'/state',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({state})});item.state=state;for(const b of actions.querySelectorAll('button'))b.setAttribute('aria-pressed',String(b===button));setMessage('状態を保存しました')}catch(e){setMessage(e.message,true)}finally{button.disabled=false}};actions.append(button)}const link=el('a','source','出典を開く ↗');link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';actions.append(link);card.append(meta,title,summary,reason,actions);list.append(card)}}
function renderLimits(data){$('dailyLimit').value=data.usage.daily_limit;$('monthlyLimit').value=data.usage.monthly_call_limit;$('monthlyUsd').value=data.usage.monthly_limit_usd;$('limitsNote').textContent=`本日 ${data.usage.today_calls} / ${data.usage.daily_limit} 回。推定費用の上限により、回数上限より早く停止する場合があります。`}
async function load(){try{const data=await api('/news/api');render(data);renderLimits(data)}catch(e){setMessage(e.message,true)}}
$('refresh').onclick=async()=>{if(busy)return;if(!confirm('OpenAIのWeb検索を1回実行します。利用料金が発生する場合があります。続けますか？'))return;busy=true;$('refresh').disabled=true;setMessage('新着ニュースを探しています…');try{const data=await api('/news/api/refresh',{method:'POST'});render(data);renderLimits(data);setMessage('更新しました')}catch(e){setMessage(e.message,true)}finally{busy=false;$('refresh').disabled=false}};
$('saveTopics').onclick=async()=>{const topics=$('topics').value.split('\n').map(x=>x.trim()).filter(Boolean);try{const data=await api('/news/api/topics',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topics})});render(data);setMessage('ジャンルを保存しました')}catch(e){setMessage(e.message,true)}};
$('saveLimits').onclick=async()=>{if(busy)return;busy=true;$('saveLimits').disabled=true;try{const data=await api('/news/api/limits',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({daily_limit:Number($('dailyLimit').value),monthly_call_limit:Number($('monthlyLimit').value),monthly_limit_usd:Number($('monthlyUsd').value)})});render(data);renderLimits(data);setMessage('検索上限を保存しました')}catch(e){setMessage(e.message,true)}finally{busy=false;$('saveLimits').disabled=false}};
load();
</script></body></html>'''
