"""
回歸測試：年級目標版本合併邏輯

Bug 描述：
  修改「獎金設定」時，若 DB 中只有部分年級已綁定到特定版本 ID（其他年級仍為 NULL），
  系統在複製目標人數時只複製有版本 ID 的年級，遺漏 NULL 年級。
  載入時若找到任一版本目標就不再 fallback，導致其他年級目標歸零，班級獎金算不出來。

測試策略：
  - 不依賴 DB（SalaryEngine load_from_db=False）
  - 直接呼叫 set_bonus_config 模擬「只有部分年級目標從 DB 載入」的狀態
  - 確認未被覆蓋的年級仍保有目標人數
"""

import pytest
from services.salary_engine import SalaryEngine


class TestGradeTargetVersionMerge:
    """年級目標版本切換不應抹除其他年級"""

    def test_partial_target_enrollment_preserves_other_grades(self, engine):
        """
        回歸測試：set_bonus_config 只傳入部分年級時，其他年級目標不應被清空。

        Bug 重現場景：
          1. load_config_from_db 只從 DB 取得「大班」（新版本只複製了大班）
          2. 現行程式碼：self._target_enrollment = {} → 再填大班 → 中/小/幼幼班消失
          3. 修復後：中/小/幼幼班應保留（從 NULL fallback 或預設值補回）
        """
        # 確認各年級的預設目標人數存在（第十條）
        assert engine.get_target_enrollment('大班', has_assistant=True) == 24
        assert engine.get_target_enrollment('中班', has_assistant=True) == 24
        assert engine.get_target_enrollment('小班', has_assistant=True) == 24
        assert engine.get_target_enrollment('幼幼班', has_assistant=True) == 15

        # 模擬 load_config_from_db 後，只有大班從 DB 取得（部分遷移情況）
        # 這對應到 set_bonus_config 的 targetEnrollment 路徑，
        # 或 load_config_from_db 的 if targets: self._target_enrollment = {} 邏輯
        engine.set_bonus_config({
            'targetEnrollment': {
                '大班': {'twoTeachers': 30, 'oneTeacher': 15, 'sharedAssistant': 22}
            }
        })

        # 大班應更新為新值
        assert engine.get_target_enrollment('大班', has_assistant=True) == 30

        # 中班/小班/幼幼班「沒有」在這次覆蓋中，應保留原有目標人數（第十條）
        # BUG 修復前：這三個年級都返回 0（因為 _target_enrollment 被清空重建）
        assert engine.get_target_enrollment('中班', has_assistant=True) == 24, \
            "中班目標人數不應因部分年級覆蓋而消失"
        assert engine.get_target_enrollment('小班', has_assistant=True) == 24, \
            "小班目標人數不應因部分年級覆蓋而消失"
        assert engine.get_target_enrollment('幼幼班', has_assistant=True) == 15, \
            "幼幼班目標人數不應因部分年級覆蓋而消失"

    def test_partial_overtime_target_preserves_other_grades(self, engine):
        """超額獎金目標人數同樣不應因部分覆蓋而消失"""
        assert engine.get_overtime_target('中班', has_assistant=True) > 0

        engine.set_bonus_config({
            'targetEnrollment': {
                '大班': {'twoTeachers': 30, 'oneTeacher': 15, 'sharedAssistant': 22}
            }
        })

        # 中班超額目標應保留
        assert engine.get_overtime_target('中班', has_assistant=True) > 0, \
            "中班超額獎金目標不應因部分年級覆蓋而消失"

    def test_full_override_still_works(self, engine):
        """所有年級都提供時，應正常全部覆蓋（不影響正常操作）"""
        engine.set_bonus_config({
            'targetEnrollment': {
                '大班': {'twoTeachers': 30, 'oneTeacher': 15, 'sharedAssistant': 22},
                '中班': {'twoTeachers': 28, 'oneTeacher': 14, 'sharedAssistant': 20},
                '小班': {'twoTeachers': 25, 'oneTeacher': 12, 'sharedAssistant': 18},
                '幼幼班': {'twoTeachers': 16, 'oneTeacher': 8, 'sharedAssistant': 13},
            }
        })
        assert engine.get_target_enrollment('大班', has_assistant=True) == 30
        assert engine.get_target_enrollment('中班', has_assistant=True) == 28
        assert engine.get_target_enrollment('幼幼班', has_assistant=True) == 16

    def test_empty_target_enrollment_in_config_does_not_wipe(self, engine):
        """bonus_config 中的 targetEnrollment 為空時，不應清空現有設定"""
        original = engine.get_target_enrollment('大班', has_assistant=True)

        # 傳入空 targetEnrollment
        engine.set_bonus_config({'targetEnrollment': {}})

        assert engine.get_target_enrollment('大班', has_assistant=True) == original, \
            "空的 targetEnrollment 不應清空現有年級目標"
