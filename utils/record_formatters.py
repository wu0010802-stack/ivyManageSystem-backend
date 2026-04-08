"""
共用的記錄格式化函式，供管理端與 portal 路由使用。
"""

from models.database import StudentAssessment, StudentIncident, Student


def assessment_to_dict(assessment: StudentAssessment, student: Student, *, include_updated_at: bool = False) -> dict:
    result = {
        "id": assessment.id,
        "student_id": assessment.student_id,
        "student_name": student.name if student else None,
        "student_no": student.student_id if student else None,
        "classroom_id": student.classroom_id if student else None,
        "semester": assessment.semester,
        "assessment_type": assessment.assessment_type,
        "domain": assessment.domain,
        "rating": assessment.rating,
        "content": assessment.content,
        "suggestions": assessment.suggestions,
        "assessment_date": assessment.assessment_date.isoformat() if assessment.assessment_date else None,
        "recorded_by": assessment.recorded_by,
        "created_at": assessment.created_at.isoformat() if assessment.created_at else None,
    }
    if include_updated_at:
        result["updated_at"] = assessment.updated_at.isoformat() if assessment.updated_at else None
    return result


def incident_to_dict(incident: StudentIncident, student: Student, *, include_updated_at: bool = False) -> dict:
    result = {
        "id": incident.id,
        "student_id": incident.student_id,
        "student_name": student.name if student else None,
        "student_no": student.student_id if student else None,
        "classroom_id": student.classroom_id if student else None,
        "incident_type": incident.incident_type,
        "severity": incident.severity,
        "occurred_at": incident.occurred_at.isoformat() if incident.occurred_at else None,
        "description": incident.description,
        "action_taken": incident.action_taken,
        "parent_notified": incident.parent_notified,
        "parent_notified_at": incident.parent_notified_at.isoformat() if incident.parent_notified_at else None,
        "recorded_by": incident.recorded_by,
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
    }
    if include_updated_at:
        result["updated_at"] = incident.updated_at.isoformat() if incident.updated_at else None
    return result
