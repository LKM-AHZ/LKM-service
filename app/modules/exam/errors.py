from core.err import NS_EXAM, ErrCode, register


class ExamErr(ErrCode):
    EXAM_NOT_FOUND = NS_EXAM.err(1)
    QUESTION_NOT_FOUND = NS_EXAM.err(2)
    ATTEMPT_NOT_FOUND = NS_EXAM.err(3)
    ATTEMPT_ALREADY_SUBMITTED = NS_EXAM.err(4)
    EXAM_NOT_PUBLISHED = NS_EXAM.err(5)
    EXAM_ALREADY_PASSED = NS_EXAM.err(6)
    ALREADY_REGISTERED = NS_EXAM.err(7)
    EXAM_NOT_OPEN = NS_EXAM.err(8)
    CERTIFICATE_NOT_FOUND = NS_EXAM.err(9)


register(
    {
        ExamErr.EXAM_NOT_FOUND: (404, "Exam not found"),
        ExamErr.QUESTION_NOT_FOUND: (404, "Question not found"),
        ExamErr.ATTEMPT_NOT_FOUND: (404, "Attempt not found"),
        ExamErr.ATTEMPT_ALREADY_SUBMITTED: (409, "Attempt already submitted"),
        ExamErr.EXAM_NOT_PUBLISHED: (403, "Exam not published"),
        ExamErr.EXAM_ALREADY_PASSED: (409, "Exam already passed"),
        ExamErr.ALREADY_REGISTERED: (409, "Already registered for this exam"),
        ExamErr.EXAM_NOT_OPEN: (403, "Exam not open yet"),
        ExamErr.CERTIFICATE_NOT_FOUND: (404, "Certificate not found"),
    }
)
