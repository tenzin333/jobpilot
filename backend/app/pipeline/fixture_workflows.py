"""Hand-authored local workflows and explicit profile bindings."""
from app.ghost_cursor.models import Action, Target, Workflow
from app.models import split_full_name


def field(key, name=None, kind="fill", labels=None, **kw):
    name = name or key
    return Action(id=key, kind=kind, binding=key, review_key=name,
                  target=Target(names=[name], labels=labels or [],
                                value="$binding" if kind == "check" and key != "privacy_acknowledged" else None), **kw)


def button(label, final=False):
    return Action(id=label, kind="final_submit" if final else "click", target=Target(role="button", labels=[label]))


def workflow(scenario: str, url: str) -> Workflow:
    contact = [field(k) for k in ("first_name", "last_name", "email", "phone")]
    if scenario == "basic":
        steps = [*contact, field("resume", kind="upload"), field("work_authorization", kind="select"),
                 field("interest", labels=["Why are you interested?"], optional=True), button("Review application")]
    elif scenario == "multistep":
        steps = [*contact, button("Continue to experience"), field("linkedin_url", optional=True),
                 field("resume", kind="upload"), field("authorized", kind="check"),
                 field("achievement", labels=["Describe one relevant achievement"]), button("Continue to review")]
    elif scenario == "weird-ui":
        steps = [field("first_name", "legal_given_name"), field("last_name", "legal_family_name"),
                 field("email", "candidate_email"), field("phone", "contact_number"),
                 field("resume", "candidate_document", "upload"), field("work_mode", kind="check"),
                 button("Review application")]
    elif scenario == "dynamic":
        steps = [Action(id="questions_loaded", kind="wait", target=Target(labels=["Country of residence"])),
                 field("country", kind="select"), field("sponsorship", kind="check"),
                 field("first_name"), field("email"), field("city"),
                 field("visa_type", when_binding="sponsorship", when_value="Yes"),
                 field("privacy_acknowledged", kind="check"), button("Review dynamic application")]
    else:
        raise ValueError("Unknown local fixture")
    return Workflow(
        name=scenario,
        url=url,
        steps=[*steps, button("Submit test application", final=True)],
        preconditions=["configured loopback destination", "prepared resume artifact", "explicit legal answers"],
        postconditions=["review values match bindings", "no receipt before confirmation", "correlated receipt after confirmation"],
    )


def profile_bindings(profile, application) -> dict:
    # Legal answers are copied only from explicit answers, never inferred.
    answer_bank = profile.answer_bank or {}
    result = {k: ("Yes" if v else "No") if isinstance(v, bool) else v
              for k, v in answer_bank.items() if isinstance(v, (str, bool))}
    normalized = {" ".join(k.replace("_", " ").casefold().split()): v for k, v in result.items()}

    def saved(*keys):
        return next((normalized[key] for key in keys if key in normalized), None)

    def yes_no(value):
        text = str(value or "").strip().casefold()
        if text in {"yes", "true", "authorized", "authorized to work"}:
            return "Yes"
        if text in {"no", "false", "not authorized"}:
            return "No"
        return None

    authorized = yes_no(saved("authorized", "authorized to work", "work authorization"))
    sponsorship = yes_no(saved("sponsorship", "require sponsorship"))
    if authorized:
        result.setdefault("authorized", authorized)
    if sponsorship:
        result.setdefault("sponsorship", sponsorship)
    work_authorization = saved("work authorization")
    if work_authorization in {"Authorized to work", "Require sponsorship"}:
        result.setdefault("work_authorization", work_authorization)
    elif authorized == "Yes":
        result.setdefault("work_authorization", "Authorized to work")
    elif sponsorship == "Yes":
        result.setdefault("work_authorization", "Require sponsorship")
    linkedin = saved("linkedin url", "linkedin")
    if linkedin:
        linkedin = str(linkedin).strip()
        if linkedin.casefold().startswith(("linkedin.com/", "www.linkedin.com/")):
            linkedin = "https://" + linkedin
        result.setdefault("linkedin_url", linkedin)
    _, inferred_first, _, inferred_last = split_full_name(getattr(profile, "full_name", ""))
    result.update(first_name=getattr(profile, "first_name", "") or inferred_first,
                  last_name=getattr(profile, "last_name", "") or inferred_last,
                  email=profile.email, phone=profile.phone)
    result["resume"] = application.resume_path
    privacy = saved("privacy acknowledged")
    result["privacy_acknowledged"] = privacy is True or str(privacy or "").lower() == "yes"
    return result
