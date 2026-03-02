"""
cover_letter.py — Generate personalised cover letters (template or OpenAI)
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Template-based cover letter
# ---------------------------------------------------------------------------

TEMPLATE = """{applicant_name}
{applicant_email}{phone_line}{location_line}
{date}

Hiring Team
{company}

Re: {job_title}

Dear Hiring Manager,

I am writing to express my enthusiastic interest in the {job_title} position at {company}. \
With {years_experience} years of professional experience in {primary_skills}, I am confident \
that my background aligns well with the requirements of this role.

Throughout my career I have developed a deep expertise in {matched_skills_sentence}. \
This direct alignment with your posting excites me — I am eager to bring these skills to bear \
for {company}.

In my previous roles I have consistently delivered results by leveraging the Microsoft technology \
stack and enterprise database solutions to build, maintain, and improve business-critical \
applications. I thrive in collaborative environments, communicate clearly with stakeholders at all \
levels, and take ownership of outcomes from requirements through deployment.

I am particularly drawn to this opportunity because it offers the chance to contribute meaningfully \
to {company}'s mission while continuing to grow in a remote-first environment. I believe a short \
conversation would quickly demonstrate how my experience can add value to your team.

Thank you for reviewing my application. I would welcome the opportunity to discuss this position \
further at your convenience.

Sincerely,
{applicant_name}
{applicant_email}{phone_sig}{linkedin_line}
"""


def generate_template_letter(job, config) -> str:
    """Fill the cover letter template with job and applicant data."""
    matched = job.get_matched_skills()

    if matched:
        if len(matched) == 1:
            matched_sentence = matched[0]
        elif len(matched) == 2:
            matched_sentence = f"{matched[0]} and {matched[1]}"
        else:
            matched_sentence = ", ".join(matched[:-1]) + f", and {matched[-1]}"
    else:
        matched_sentence = "enterprise software development, C#/.NET, and SQL databases"

    # Primary skills summary
    primary = ", ".join(matched[:3]) if matched else "C#, .NET, and SQL Server"

    phone_line = f"\n{config.APPLICANT_PHONE}" if config.APPLICANT_PHONE else ""
    location_line = f"\n{config.APPLICANT_LOCATION}" if config.APPLICANT_LOCATION else ""
    phone_sig = f"\n{config.APPLICANT_PHONE}" if config.APPLICANT_PHONE else ""
    linkedin_line = f"\n{config.APPLICANT_LINKEDIN}" if config.APPLICANT_LINKEDIN else ""

    letter = TEMPLATE.format(
        applicant_name=config.APPLICANT_NAME,
        applicant_email=config.APPLICANT_EMAIL,
        phone_line=phone_line,
        location_line=location_line,
        date=datetime.now().strftime("%B %d, %Y"),
        company=job.company or "the Company",
        job_title=job.title,
        years_experience=config.YEARS_EXPERIENCE,
        primary_skills=primary,
        matched_skills_sentence=matched_sentence,
        phone_sig=phone_sig,
        linkedin_line=linkedin_line,
    )
    return letter.strip()


def generate_ai_letter(job, config) -> str:
    """
    Generate a cover letter using OpenAI (requires OPENAI_API_KEY in config).
    Falls back to the template version if the API call fails.
    """
    if not config.OPENAI_API_KEY:
        return generate_template_letter(job, config)

    try:
        import openai  # only imported when needed
        client = openai.OpenAI(api_key=config.OPENAI_API_KEY)

        matched = job.get_matched_skills()
        skills_str = ", ".join(matched) if matched else "C#, .NET, Oracle, SQL Server"

        prompt = (
            f"Write a professional, concise cover letter for the following job.\n\n"
            f"Job Title: {job.title}\n"
            f"Company: {job.company or 'the Company'}\n"
            f"Matched Skills: {skills_str}\n\n"
            f"Applicant Profile:\n"
            f"  Name: {config.APPLICANT_NAME}\n"
            f"  Email: {config.APPLICANT_EMAIL}\n"
            f"  Years of Experience: {config.YEARS_EXPERIENCE}\n"
            f"  Location: {config.APPLICANT_LOCATION}\n\n"
            f"Requirements:\n"
            f"- Address it to 'Dear Hiring Manager'\n"
            f"- Mention the specific job title and company name\n"
            f"- Highlight the matched skills naturally\n"
            f"- Keep it under 350 words\n"
            f"- Professional but warm tone\n"
            f"- End with a call to action\n"
            f"- Do NOT include a salutation header block (just start with 'Dear Hiring Manager')\n"
        )

        response = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert career coach writing professional cover letters."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=600,
            temperature=0.7,
        )
        body = response.choices[0].message.content.strip()

        # Wrap with header/footer
        header = (
            f"{config.APPLICANT_NAME}\n"
            f"{config.APPLICANT_EMAIL}"
        )
        if config.APPLICANT_PHONE:
            header += f"\n{config.APPLICANT_PHONE}"
        if config.APPLICANT_LOCATION:
            header += f"\n{config.APPLICANT_LOCATION}"
        header += f"\n{datetime.now().strftime('%B %d, %Y')}\n\n{job.company or 'Hiring Team'}\nRe: {job.title}\n"

        footer = f"\n\nSincerely,\n{config.APPLICANT_NAME}"
        if config.APPLICANT_EMAIL:
            footer += f"\n{config.APPLICANT_EMAIL}"
        if config.APPLICANT_LINKEDIN:
            footer += f"\n{config.APPLICANT_LINKEDIN}"

        return (header + body + footer).strip()

    except Exception as exc:
        logger.warning("OpenAI cover letter failed: %s — using template", exc)
        return generate_template_letter(job, config)


def generate(job, config, use_ai: bool = False) -> str:
    """Public entry point. Use AI if key is set and use_ai=True."""
    if use_ai and config.OPENAI_API_KEY:
        return generate_ai_letter(job, config)
    return generate_template_letter(job, config)
