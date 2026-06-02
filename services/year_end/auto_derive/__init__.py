"""年終獎金 E化 階段2 — auto-derive 子套件。

各特別獎金欄位的「自動推導」邏輯，每個 derive 函式各自一檔、單一責任：
  - after_class_award.py : ① 才藝鼓勵（報名人次 × 班別單價）

編排層 derive_all（呼叫各 derive 並彙總 report）由 B7 補上，此處先留空殼。

override 慣例（橫跨 B2-B7）：auto-derive 寫入的 special_bonus_items 一律以
``source_ref`` 前綴 ``"auto:"`` 標記為「自動」。upsert 時：
  1. 以 uq 鍵 (year_end_cycle_id, employee_id, bonus_type, period_label) 查既有 row。
  2. 既有 row 的 source_ref **不是** ``auto:`` 開頭（None 或使用者手填）→ 手動筆，SKIP。
  3. 既有 row 的 source_ref 以 ``auto:`` 開頭 → 上次自動寫的，UPDATE。
  4. 不存在 → 新建。
"""
