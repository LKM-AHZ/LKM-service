from core.err import NS_EXAM, ErrCode, register, register_unique_constraint


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
    GROUP_NOT_FOUND = NS_EXAM.err(10)
    GROUP_KEY_TAKEN = NS_EXAM.err(11)
    NOT_REGISTERED = NS_EXAM.err(12)
    PREREQUISITE_NOT_MET = NS_EXAM.err(13)
    ATTEMPT_EXPIRED = NS_EXAM.err(14)
    ATTEMPT_NOT_REVIEWABLE = NS_EXAM.err(15)
    REVIEW_TARGET_NOT_MANUAL = NS_EXAM.err(16)
    TRIAL_NOT_FOUND = NS_EXAM.err(17)
    PRACTICE_EXAM_REQUIRED = NS_EXAM.err(18)
    MAX_ATTEMPTS_REACHED = NS_EXAM.err(19)
    WRONG_ANSWER_NOT_FOUND = NS_EXAM.err(20)
    PRACTICE_NO_QUESTIONS = NS_EXAM.err(21)


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
        ExamErr.GROUP_NOT_FOUND: (404, "Question group not found"),
        ExamErr.GROUP_KEY_TAKEN: (409, "Question group key already exists"),
        ExamErr.NOT_REGISTERED: (403, "Registration required for this exam"),
        ExamErr.PREREQUISITE_NOT_MET: (403, "Exam prerequisites not met"),
        ExamErr.ATTEMPT_EXPIRED: (409, "Attempt expired"),
        ExamErr.ATTEMPT_NOT_REVIEWABLE: (409, "Attempt is not awaiting review"),
        ExamErr.REVIEW_TARGET_NOT_MANUAL: (400, "Only manual questions can be reviewed"),
        ExamErr.TRIAL_NOT_FOUND: (404, "Trial not found"),
        ExamErr.PRACTICE_EXAM_REQUIRED: (400, "Practice attempts require a practice exam"),
        ExamErr.MAX_ATTEMPTS_REACHED: (409, "Maximum number of attempts reached"),
        ExamErr.WRONG_ANSWER_NOT_FOUND: (404, "Wrong answer not found"),
        ExamErr.PRACTICE_NO_QUESTIONS: (400, "No practice questions available"),
    }
)

register_unique_constraint("exam_registrations", ExamErr.ALREADY_REGISTERED)
