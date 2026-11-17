(function(){
  const importBtn = document.querySelector("#import-reports");
  const payloadEl = document.querySelector("#reports-payload");
  const importOut = document.querySelector("#import-result");
  const disputesEl = document.querySelector("#disputes");
  const vouchersEl = document.querySelector("#vouchers");
  const feedback = document.querySelector("#feedback");
  if(!importBtn || !disputesEl || !vouchersEl) return;

  const short = (digest) => String(digest).slice(0, 8);
  const uid = () => `ADJ-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const esc = (text) => String(text).replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));

  async function api(path, options){
    const response = await fetch(path, options);
    const data = await response.json();
    if(!response.ok){
      const detail = data.detail || {};
      throw new Error(detail.message || detail.code || "请求失败");
    }
    return data;
  }

  async function refresh(){
    try{
      const [disputeData, voucherData] = await Promise.all([
        api("/api/documents/disputes"),
        api("/api/documents/vouchers")
      ]);
      renderDisputes(disputeData);
      renderVouchers(voucherData);
    }catch(error){ feedback.textContent = error.message; }
  }

  function renderDisputes(data){
    const reasons = data.reasons || {};
    document.querySelector("#dispute-count").textContent = `${data.disputes.length} 票`;
    disputesEl.innerHTML = "";
    if(!data.disputes.length){
      disputesEl.innerHTML = "<p class='empty'>暂无争议票</p>";
      return;
    }
    for(const dispute of data.disputes){
      const box = document.createElement("div");
      box.className = "dispute";
      const options = Object.entries(reasons).map(([code, label]) => `<option value="${esc(code)}">${esc(label)}</option>`).join("");
      box.innerHTML = `
        <div class="dispute__head"><b>${esc(dispute.ticket_key)}</b><span class="state state--warn">暂停勾稽 · v${dispute.version}</span></div>
        <div class="dispute__candidates"></div>
        <div class="dispute__form">
          <select class="reason-code">${options}</select>
          <input class="reason-text" placeholder="原件说明（可选）">
        </div>`;
      const list = box.querySelector(".dispute__candidates");
      for(const candidate of dispute.candidates){
        const row = document.createElement("div");
        row.className = "candidate";
        row.innerHTML = `
          <div class="candidate__meta">
            <code>${esc(short(candidate.digest))}</code>
            <span>${esc(candidate.sources.join("、"))}</span>
            <span class="num">${esc(candidate.total_amount)}</span>
            <span>${candidate.lines.length} 行 · 重发 ${candidate.seen_count} 次</span>
          </div>
          <button class="btn btn--primary candidate__pick">裁决此票</button>`;
        const adjudicationId = uid();
        row.querySelector(".candidate__pick").addEventListener("click", async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          try{
            const result = await api("/api/documents/adjudicate", {
              method: "POST",
              headers: {"content-type": "application/json"},
              body: JSON.stringify({
                ticket_key: dispute.ticket_key,
                digest: candidate.digest,
                reason_code: box.querySelector(".reason-code").value,
                reason_text: box.querySelector(".reason-text").value,
                base_version: dispute.version,
                adjudication_id: adjudicationId
              })
            });
            feedback.textContent = `已裁决 ${result.ticket_key}（${result.replayed ? "重试复用" : "落账完成"}）`;
            await refresh();
          }catch(error){
            feedback.textContent = `裁决被拒：${error.message}`;
            button.disabled = false;
          }
        });
        list.appendChild(row);
      }
      disputesEl.appendChild(box);
    }
  }

  function renderVouchers(data){
    document.querySelector("#voucher-count").textContent = `${data.vouchers.length} 票`;
    const parts = [];
    for(const voucher of data.vouchers){
      const basis = voucher.adjudication
        ? `裁决 ${esc(voucher.adjudication.adjudication_id)} · ${esc(voucher.adjudication.reason_code)}`
        : "首报采信";
      parts.push(`<div class="voucher"><b>${esc(voucher.ticket_key)}</b><code>${esc(short(voucher.digest))}</code><span>${esc(voucher.sources.join("、"))}</span><span class="num">${esc(voucher.total_amount)}</span><small>${basis}</small></div>`);
    }
    for(const key of data.suspended){
      parts.push(`<div class="voucher voucher--paused"><b>${esc(key)}</b><span class="state state--warn">争议未决 · 暂停勾稽</span></div>`);
    }
    vouchersEl.innerHTML = parts.join("") || "<p class='empty'>暂无有效凭据</p>";
  }

  importBtn.addEventListener("click", async () => {
    importBtn.disabled = true;
    try{
      const payload = JSON.parse(payloadEl.value);
      const result = await api("/api/documents/reports", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify(payload)
      });
      importOut.textContent = JSON.stringify(result, null, 2);
      feedback.textContent = "导入完成";
      await refresh();
    }catch(error){
      importOut.textContent = error.message;
      feedback.textContent = "导入失败";
    }finally{
      importBtn.disabled = false;
    }
  });

  refresh();
})();
