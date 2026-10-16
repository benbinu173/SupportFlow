import { ApiError, ERROR_CODES } from '@/services/api/errors'

/**
 * The password policy, as one module.
 *
 * Three things need to agree about it — the submit-time check, the checklist
 * beside the field, and the server — and they agree by reading the same two
 * numbers rather than by each writing them down. `PASSWORD_MIN_LENGTH` and
 * `PASSWORD_MAX_LENGTH` mirror `app/core/config.py`, which is where
 * `app/schemas/fields.py` reads them from, so the server's values are settings
 * and these are its defaults. Being wrong in this direction costs a round trip
 * that reports a rule the form already knew about; being wrong the other way
 * costs a password the user was told was fine and then refused.
 *
 * Length only — no "one uppercase" and no "one symbol" — matching the policy.
 * NIST SP 800-63B's reasoning is that character-class rules push people towards
 * `P@ssw0rd1`, which satisfies the classes and not the point; a length floor is
 * the one requirement that adds entropy without teaching a pattern.
 */
export const PASSWORD_MIN_LENGTH = 12
export const PASSWORD_MAX_LENGTH = 128

/**
 * The policy as a list a person can read, and watch themselves satisfy.
 *
 * It is two items long because the policy is two items long. There is no third
 * row invented to make the checklist look thorough — a rule the server does not
 * enforce is a rule the interface made up, and the whole list stops being
 * trustworthy the first time somebody satisfies every row and is refused
 * anyway.
 */
export const PASSWORD_RULES = [
  {
    label: `At least ${String(PASSWORD_MIN_LENGTH)} characters`,
    satisfied: (password: string) => password.length >= PASSWORD_MIN_LENGTH,
  },
  {
    label: `No more than ${String(PASSWORD_MAX_LENGTH)} characters`,
    satisfied: (password: string) => password.length <= PASSWORD_MAX_LENGTH,
  },
] as const

/**
 * The length floor, checked on submit, as an error the form already knows how
 * to render.
 *
 * **Why the floor needs code at all, when the ceiling does not.** `maxLength`
 * truncates as the user types, so a 129th character never reaches the DOM, the
 * form state, or the request — the ceiling is enforced by the browser and this
 * function is a second opinion about a value that cannot occur. `minLength` is
 * not: it is a constraint the browser checks *at submission*, and the form
 * carries `noValidate` so that the server's validation messages are the ones
 * users see rather than two different vocabularies in the same slot. `noValidate`
 * disables that submission-time check along with the rest, which leaves the
 * floor unenforced anywhere in the client. So the asymmetry is deliberate: the
 * ceiling rides on an attribute, the floor needs a function.
 *
 * The message is shaped as an `ApiError` with one field error rather than
 * invented copy in a separate channel, because the form already renders field
 * errors from the server and a local refusal should not look like a different
 * kind of news. `status: null` is honest: no request was made.
 *
 * An empty password returns `null`. "Required" is a different complaint with a
 * different message, and the server owns both.
 */
export function passwordLengthProblem(password: string): ApiError | null {
  if (password.length === 0) {
    return null
  }

  const problem =
    password.length < PASSWORD_MIN_LENGTH
      ? `Use at least ${String(PASSWORD_MIN_LENGTH)} characters.`
      : password.length > PASSWORD_MAX_LENGTH
        ? `Use no more than ${String(PASSWORD_MAX_LENGTH)} characters.`
        : null

  if (problem === null) {
    return null
  }

  return new ApiError({
    message: 'The request could not be processed.',
    code: ERROR_CODES.VALIDATION_ERROR,
    status: null,
    fieldErrors: [{ field: 'password', message: problem }],
  })
}
