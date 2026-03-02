"""
skills_taxonomy.py — Curated skills taxonomy for the Skills Picker modal.

Structure: Category → Subcategory → [skill, ...]
Skills are sourced from industry-standard frameworks (O*NET, ESCO, LinkedIn Skills,
CompTIA, Microsoft certifications) and reflect current US job-market terminology.
"""

SKILLS_TAXONOMY = {

    "Software Development": {
        "Microsoft Stack": [
            "C#", ".NET", ".NET Core", "ASP.NET Core", "ASP.NET MVC", "Blazor",
            "SignalR", "Entity Framework", "LINQ", "WPF", "MAUI", "WCF",
            "Visual Studio", "Visual Studio Code", "NuGet", "MSTest", "xUnit", "NUnit",
        ],
        "Web & Frontend": [
            "HTML", "CSS", "JavaScript", "TypeScript", "React", "Angular", "Vue.js",
            "Bootstrap", "Tailwind CSS", "REST API", "GraphQL", "jQuery",
            "Webpack", "Vite", "Sass / SCSS", "Next.js", "Nuxt.js",
        ],
        "Backend Languages & Frameworks": [
            "Python", "Java", "Go", "Rust", "Ruby", "PHP", "C++", "Scala", "Kotlin",
            "Node.js", "Spring Boot", "Django", "FastAPI", "Flask", "Express.js",
            "Laravel", "Ruby on Rails",
        ],
        "Mobile Development": [
            "Android", "iOS", "Swift", "Kotlin", "React Native",
            "Flutter", "Xamarin", "MAUI", "Ionic",
        ],
    },

    "Data & Databases": {
        "SQL & Relational Databases": [
            "SQL Server", "T-SQL", "MSSQL", "SSRS", "SSIS", "SSAS",
            "Oracle DB", "PL/SQL", "Oracle APEX", "Oracle Forms", "Oracle EBS",
            "MySQL", "PostgreSQL", "SQLite", "MariaDB", "DB2",
        ],
        "NoSQL & Big Data": [
            "MongoDB", "Redis", "Cassandra", "Elasticsearch", "DynamoDB",
            "Apache Kafka", "Apache Spark", "Hadoop", "Snowflake",
            "Databricks", "Hive", "HBase",
        ],
        "Analytics & Business Intelligence": [
            "Power BI", "Tableau", "Looker", "Qlik",
            "Excel", "Power Query", "DAX", "Power Pivot",
            "SSRS", "Crystal Reports",
        ],
        "Data Science & ML": [
            "Machine Learning", "Deep Learning", "TensorFlow", "PyTorch",
            "Scikit-learn", "Pandas", "NumPy", "Matplotlib", "Jupyter",
            "R", "SAS", "SPSS", "Natural Language Processing",
        ],
    },

    "Cloud & DevOps": {
        "Microsoft Azure": [
            "Azure", "Azure DevOps", "Azure Functions", "Azure SQL",
            "Azure Kubernetes Service", "Azure Blob Storage",
            "Azure Active Directory", "Azure Logic Apps", "Azure Service Bus",
            "Azure Pipelines", "Azure Monitor",
        ],
        "AWS": [
            "AWS", "EC2", "S3", "Lambda", "RDS", "EKS", "ECS",
            "CloudFormation", "CloudWatch", "IAM", "Route 53", "SQS / SNS",
        ],
        "Google Cloud & Other": [
            "Google Cloud Platform", "GCP", "BigQuery", "GKE", "Cloud Run",
            "Oracle Cloud", "OCI", "IBM Cloud", "DigitalOcean", "Heroku",
        ],
        "DevOps & Containers": [
            "Docker", "Kubernetes", "Terraform", "Ansible", "Puppet", "Chef",
            "CI/CD", "GitHub Actions", "Jenkins", "GitLab CI", "CircleCI",
            "Helm", "ArgoCD", "Prometheus", "Grafana", "Datadog",
        ],
        "Version Control & Collaboration": [
            "Git", "GitHub", "GitLab", "Bitbucket", "Azure Repos", "SVN",
            "Jira", "Confluence", "Trello", "Slack", "Microsoft Teams",
        ],
    },

    "IT & Infrastructure": {
        "Networking": [
            "TCP/IP", "DNS", "DHCP", "VPN", "Firewall", "Network Administration",
            "Cisco", "Juniper", "Palo Alto", "Wireshark", "Load Balancing",
            "F5", "SD-WAN", "BGP / OSPF",
        ],
        "Systems Administration": [
            "Windows Server", "Active Directory", "Group Policy", "Linux", "Ubuntu",
            "Red Hat / RHEL", "PowerShell", "Bash", "VMware", "Hyper-V",
            "vSphere", "LDAP", "Exchange Server", "Office 365 Admin",
        ],
        "Cybersecurity": [
            "Cybersecurity", "Penetration Testing", "SIEM", "IAM",
            "CompTIA Security+", "CISSP", "CEH", "CISA",
            "SOC", "Vulnerability Assessment", "OWASP",
            "Zero Trust", "CrowdStrike", "Splunk", "Incident Response",
        ],
        "Monitoring & ITSM": [
            "ServiceNow", "JIRA Service Management", "Nagios", "Datadog",
            "New Relic", "PagerDuty", "Prometheus", "Grafana",
            "ITIL", "Help Desk Management", "SLA Management",
        ],
        "Hardware & Support": [
            "Hardware Troubleshooting", "PC Build & Repair", "Printer Support",
            "Mobile Device Management (MDM)", "Intune", "SCCM",
            "Imaging & Deployment", "Asset Management",
        ],
    },

    "Customer Service": {
        "Support Skills": [
            "Customer Service", "Customer Support", "Technical Support",
            "Help Desk", "Service Desk", "Tier 1 Support", "Tier 2 Support",
            "Tier 3 Support", "Call Center", "Chat Support",
            "Email Support", "Inbound / Outbound Calls", "Complaint Resolution",
        ],
        "CRM & Ticketing Tools": [
            "Salesforce", "Zendesk", "Freshdesk", "ServiceNow",
            "HubSpot", "Zoho CRM", "Dynamics 365", "Intercom",
            "LiveChat", "Freshservice", "SolarWinds",
        ],
        "Customer Success": [
            "Customer Success", "Account Management", "Client Onboarding",
            "Client Relations", "Retention Strategies", "Upselling / Cross-selling",
            "CSAT", "NPS", "Churn Reduction", "Voice of the Customer",
        ],
    },

    "Project & Business": {
        "Project Management": [
            "Agile", "Scrum", "Kanban", "SAFe", "Lean", "Waterfall",
            "PMP", "PRINCE2", "PMO", "Risk Management",
            "Stakeholder Management", "Budget Management", "Change Management",
        ],
        "Business Analysis": [
            "Business Analysis", "Requirements Gathering", "Process Improvement",
            "BPMN", "UML", "User Stories", "Acceptance Criteria",
            "Gap Analysis", "Feasibility Studies", "SWOT Analysis",
            "As-Is / To-Be Mapping", "Wireframing",
        ],
        "ERP & Enterprise Systems": [
            "SAP", "Oracle EBS", "Oracle Fusion", "Microsoft Dynamics",
            "Dynamics 365", "NetSuite", "Workday", "PeopleSoft",
        ],
        "Productivity & Office": [
            "Microsoft Office", "Excel", "Word", "PowerPoint", "Outlook",
            "SharePoint", "Teams", "OneNote", "Visio", "Access",
            "Google Workspace", "Docs", "Sheets", "Slides",
        ],
    },

    "Soft Skills": {
        "Interpersonal": [
            "Communication", "Teamwork", "Collaboration", "Leadership",
            "Mentoring", "Coaching", "Presentation Skills",
            "Negotiation", "Conflict Resolution", "Empathy",
        ],
        "Professional Qualities": [
            "Problem Solving", "Critical Thinking", "Attention to Detail",
            "Time Management", "Self-Motivated", "Adaptability",
            "Multitasking", "Remote Work", "Work Ethic",
            "Creative Thinking", "Continuous Learning",
        ],
    },
}


def get_taxonomy():
    """Return the full skills taxonomy as a plain JSON-serialisable dict."""
    return SKILLS_TAXONOMY


def get_all_skills_flat() -> set:
    """Return a lowercase set of every skill in the static taxonomy (for fast lookup)."""
    flat = set()
    for cat in SKILLS_TAXONOMY.values():
        for skills in cat.values():
            for s in skills:
                flat.add(s.lower())
    return flat
