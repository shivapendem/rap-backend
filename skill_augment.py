"""
skill_augment.py — rules-only (no AI) "add every JD skill" step for
generated (tailored) resumes.

After a tailored resume is built, the requirement's skills are added:
  1. TECHNICAL PROFICIENCIES — EVERY JD skill the resume doesn't list yet,
     in its category row (existing row if present, else a new row);
  2. WORK EXPERIENCE — at most 3 new points (MAX_NEW_BULLETS) across the
     two most recent projects (max 2 per project). Each point covers one
     category (up to 4 skills); the biggest groups of new skills win.
     Skills already named in the candidate's own bullets get no new point;
  3. PROFESSIONAL SUMMARY — 1-2 sentences naming the skills the new points
     introduced, written in the base summary's own form: a paragraph gets
     the sentences appended inside it, a bulleted summary gets bullets.
     The base summary text itself is never changed.

Everything added is recorded in resume_data["jd_added"] so the review
dialog can highlight it and remove any single item (✕) before Finalize.
Each added bullet / summary sentence stores its template + skills, so
removing one skill re-renders the sentence without it (join_natural()
MUST stay identical to joinNatural() in ResumeRichPreview.tsx).

Never added (listed in missing_skills / the Skills Gap box instead):
  - certifications (PMP, AZ-104, CSM ...) — a certificate is verifiable
    and must not be claimed;
  - placeholder words the requirement parser sometimes emits as a
    "skill" ("Developer", "Admin", "Support" ...).
A skill released after BOTH recent projects ended gets the skill-table
entry only — no project point (it could not have been used on either
project); this is listed in jd_added["no_bullet"].
Added text never states years of experience.
"""
from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Knowledge base: canonical skill -> category, domains, release (year, month),
# single-skill bullet template ({skills} placeholder).
# Aliases here are ONLY used to look up the template/category/release date —
# never to decide "the candidate already has this skill" (see same_skill()).
# ---------------------------------------------------------------------------
G = "general"


def _k(cat, domains, year, template, aliases=(), month=1):
    return {"cat": cat, "domains": set(domains), "rel": (year, month), "t": template, "aliases": list(aliases)}


KB: dict[str, dict] = {
    # languages
    "java": _k("Programming Languages", ["java"], 1996, "Developed and maintained backend modules in {skills}, following enterprise coding standards and peer code reviews.", ["core java", "java 8/11/17", "advanced java concepts", "j2ee", "jee"]),
    "python": _k("Programming Languages", ["python", "data", "ml", "devops"], 1991, "Wrote {skills} scripts and services to automate data handling and operational tasks.", ["python development", "python3"]),
    "javascript": _k("Programming Languages", ["frontend", "java", "python"], 1995, "Built interactive UI features in {skills}, handling client-side validation and API integration.", ["js", "es6"]),
    "typescript": _k("Programming Languages", ["frontend"], 2012, "Developed strongly typed UI components and services in {skills}."),
    "c#": _k("Programming Languages", ["dotnet"], 2002, "Developed application features and APIs in {skills} on the .NET platform.", ["csharp"]),
    "go": _k("Programming Languages", ["devops", "cloud"], 2012, "Wrote {skills} utilities and services for platform automation and tooling.", ["golang", "go lang"]),
    "scala": _k("Programming Languages", ["data"], 2004, "Developed {skills} jobs for distributed data transformations."),
    "swift": _k("Mobile Development", ["mobile"], 2014, "Built iOS screens and features in {skills}, integrating with backend REST APIs."),
    "kotlin": _k("Mobile Development", ["mobile", "java"], 2016, "Developed Android features in {skills} following MVVM patterns."),
    "sql": _k("Databases & Data Stores", ["data", "db", "java", "python", "dotnet", "qa", G], 1986, "Wrote and optimized {skills} queries, joins and views to support reporting and application data needs.", ["advanced sql", "spark sql"]),
    "pl/sql": _k("Databases & Data Stores", ["db", "data"], 1992, "Developed {skills} stored procedures, functions and packages for data processing."),
    "stored procedures": _k("Databases & Data Stores", ["db", "data", "dotnet", "java"], 1990, "Wrote and tuned {skills} for data processing and reporting."),
    "bash": _k("Programming Languages", ["devops", "cloud"], 1989, "Automated routine operational tasks with {skills} scripts.", ["shell scripting", "shell scripting (linux/unix)"]),
    "powershell": _k("Programming Languages", ["devops", "cloud", "dotnet", "m365"], 2006, "Automated administration and deployment tasks using {skills} scripts.", ["powershell scripting"]),
    "groovy": _k("Programming Languages", ["devops", "java"], 2007, "Wrote {skills} scripts for Jenkins pipeline stages and build automation."),
    # java ecosystem
    "spring boot": _k("Backend Frameworks & APIs", ["java"], 2014, "Built RESTful microservices using {skills} with layered controller, service and repository design.", ["springboot"]),
    "spring": _k("Backend Frameworks & APIs", ["java"], 2004, "Used {skills} dependency injection and MVC to structure backend application components.", ["spring framework", "spring mvc", "spring framework (spring mvc, spring security, aspects)"]),
    "spring security": _k("Backend Frameworks & APIs", ["java"], 2008, "Secured REST endpoints with {skills}, configuring authentication and role-based authorization."),
    "spring batch": _k("Backend Frameworks & APIs", ["java"], 2008, "Developed {skills} jobs for scheduled bulk data processing."),
    "hibernate": _k("Backend Frameworks & APIs", ["java"], 2001, "Mapped entities and repositories with {skills} for relational data access.", ["jpa"]),
    "jdbc": _k("Backend Frameworks & APIs", ["java"], 1997, "Implemented database access with {skills} and connection pooling."),
    "junit": _k("Testing & Monitoring", ["java", "qa"], 2002, "Wrote {skills} unit tests for service and controller layers to protect against regressions.", ["mockito", "testng"]),
    "maven": _k("DevOps & CI/CD", ["java"], 2004, "Managed builds and dependencies with {skills}.", ["gradle"]),
    "microservices": _k("Backend Frameworks & APIs", ["java", "python", "dotnet", "devops"], 2014, "Built independently deployable services in a {skills} architecture with clear API contracts.", ["microservices architecture"]),
    "rest api": _k("Backend Frameworks & APIs", ["java", "python", "dotnet", "frontend", "qa", "salesforce", "data"], 2000, "Designed and consumed {skills} with proper status codes, validation and error handling.", ["rest apis", "restful apis", "rest", "rest services", "restful web services", "restful", "apis", "api development", "api integration", "rest api development and integration", "web services", "web services (soap & rest)"]),
    "graphql": _k("Backend Frameworks & APIs", ["frontend", "java", "python"], 2015, "Built and consumed {skills} queries and schemas for front-end data needs."),
    "soap": _k("Backend Frameworks & APIs", ["java", "dotnet", "salesforce", "sap"], 2000, "Integrated with {skills} web services for partner and legacy system data exchange.", ["soap api", "soap apis"]),
    "grpc": _k("Backend Frameworks & APIs", ["java", "python", "devops"], 2016, "Implemented service-to-service communication over {skills}.", ["protobuf"]),
    "kafka": _k("Big Data & Streaming", ["java", "python", "data", "devops"], 2011, "Produced and consumed {skills} events to decouple services and process data asynchronously.", ["kafka streams"]),
    "rabbitmq": _k("Big Data & Streaming", ["java", "python", "dotnet"], 2007, "Used {skills} queues for asynchronous messaging between services.", ["jms/amqp"]),
    "event-driven architecture": _k("Backend Frameworks & APIs", ["java", "python", "data", "dotnet"], 2010, "Built message-based components following {skills} principles.", ["event driven design", "event driven systems"]),
    ".net": _k("Backend Frameworks & APIs", ["dotnet"], 2002, "Developed application features and services on {skills}.", [".net core", "asp.net", "dotnet"]),
    # frontend
    "react": _k("Frontend Frameworks & Tools", ["frontend", "java", "python"], 2013, "Built reusable {skills} components with state management and API integration.", ["react js", "reactjs", "react.js"]),
    "angular": _k("Frontend Frameworks & Tools", ["frontend", "java", "dotnet"], 2016, "Developed {skills} components, services and routing for single-page application screens.", ["angular 13+"]),
    "angularjs": _k("Frontend Frameworks & Tools", ["frontend", "java"], 2010, "Maintained {skills} controllers and directives in single-page application modules."),
    "next.js": _k("Frontend Frameworks & Tools", ["frontend"], 2016, "Built server-rendered pages with {skills}.", ["nextjs"]),
    "node.js": _k("Backend Frameworks & APIs", ["frontend", "java", "python"], 2009, "Built backend endpoints and tooling with {skills}.", ["nodejs"]),
    "html": _k("Web Technologies", ["frontend", "java", "python", "dotnet"], 1993, "Built responsive page layouts with {skills}.", ["html5"]),
    "css": _k("Web Technologies", ["frontend", "java", "python", "dotnet"], 1996, "Styled responsive layouts with {skills}.", ["css3"]),
    "jest": _k("Testing & QA", ["frontend"], 2014, "Wrote {skills} unit tests for UI components.", ["react testing library", "vitest", "karmajs"]),
    # cloud
    "aws": _k("Cloud Platforms", ["cloud", "devops", "data", "java", "python", "ml"], 2006, "Deployed and operated application components on {skills}, working with core compute, storage and IAM services.", ["amazon web services", "aws ec2", "aws s3", "s3", "aws iam"]),
    "aws lambda": _k("Cloud Platforms", ["cloud", "devops", "python", "java", "data"], 2014, "Built serverless functions on {skills} for event-driven processing.", ["lambda"]),
    "aws kinesis": _k("Big Data & Streaming", ["data", "cloud"], 2013, "Ingested streaming data with {skills} for downstream processing."),
    "aws sqs": _k("Cloud Platforms", ["cloud", "java", "python"], 2006, "Used {skills} queues to decouple producers and consumers."),
    "azure": _k("Cloud Platforms", ["cloud", "devops", "data", "dotnet", "java", "python", "ml"], 2010, "Deployed and supported workloads on {skills}, working with compute, storage, networking and identity services.", ["microsoft azure"]),
    "gcp": _k("Cloud Platforms", ["cloud", "devops", "data", "ml"], 2008, "Deployed and supported services on {skills}, using compute, storage and IAM.", ["google cloud", "google cloud platform (gcp)", "gcp cloud", "google cloud platform"]),
    "azure functions": _k("Cloud Platforms", ["cloud", "dotnet", "python"], 2016, "Built {skills} for scheduled and event-triggered processing."),
    "azure data factory": _k("Data Engineering & Governance", ["data"], 2015, "Built {skills} pipelines to orchestrate ingestion and transformation workflows.", ["azure data factory (adf)", "adf"]),
    "azure kubernetes service": _k("DevOps & CI/CD", ["devops", "cloud"], 2018, "Deployed and operated containerized workloads on {skills}.", ["azure kubernetes service (aks)", "aks"], month=6),
    "aws eks": _k("DevOps & CI/CD", ["devops", "cloud"], 2018, "Deployed and operated containerized workloads on {skills}.", ["aws ecs/eks", "eks"], month=6),
    "gke": _k("DevOps & CI/CD", ["devops", "cloud"], 2015, "Deployed and operated containerized workloads on {skills}.", ["gke administration"]),
    "cloudformation": _k("DevOps & CI/CD", ["devops", "cloud"], 2011, "Provisioned AWS resources with {skills} templates."),
    "bicep": _k("DevOps & CI/CD", ["devops", "cloud"], 2020, "Defined Azure infrastructure as code with {skills} templates.", ["arm templates"], month=8),
    "cloudwatch": _k("Testing & Monitoring", ["devops", "cloud"], 2009, "Configured {skills} metrics, logs and alarms for application monitoring."),
    "azure monitor": _k("Testing & Monitoring", ["devops", "cloud"], 2016, "Set up {skills} alerts and dashboards for platform health.", ["application insights", "log analytics"]),
    # devops
    "docker": _k("DevOps & CI/CD", ["devops", "cloud", "java", "python", "dotnet", "data", "ml"], 2013, "Containerized applications with {skills}, writing Dockerfiles and managing images."),
    "kubernetes": _k("DevOps & CI/CD", ["devops", "cloud", "java", "python"], 2014, "Deployed and managed services on {skills}, configuring deployments, services and resource limits.", ["k8s"]),
    "openshift": _k("DevOps & CI/CD", ["devops", "cloud", "java"], 2011, "Deployed and supported applications on {skills} clusters.", ["red hat openshift"]),
    "helm": _k("DevOps & CI/CD", ["devops", "cloud"], 2016, "Packaged and released Kubernetes workloads with {skills} charts."),
    "terraform": _k("DevOps & CI/CD", ["devops", "cloud"], 2014, "Provisioned cloud infrastructure with {skills} modules and managed state across environments."),
    "infrastructure as code": _k("DevOps & CI/CD", ["devops", "cloud"], 2011, "Managed environments through {skills} with version-controlled, repeatable provisioning.", ["infrastructure as code (iac)", "iac"]),
    "ansible": _k("DevOps & CI/CD", ["devops", "cloud"], 2012, "Automated server configuration with {skills} playbooks."),
    "puppet": _k("DevOps & CI/CD", ["devops"], 2005, "Managed server configuration with {skills}.", ["chef"]),
    "jenkins": _k("DevOps & CI/CD", ["devops", "java", "qa"], 2011, "Built and maintained {skills} pipelines for build, test and deployment stages."),
    "ci/cd": _k("DevOps & CI/CD", ["devops", "java", "python", "dotnet", "qa", "data", "salesforce", "frontend"], 2010, "Built and maintained {skills} covering build, automated tests and deployments.", ["ci/cd pipelines", "ci/cd pipeline", "continuous integration", "continuous delivery", "ci/cd pipeline development", "automated deployment pipelines", "deployment automation"]),
    "azure devops": _k("DevOps & CI/CD", ["devops", "dotnet", "cloud", "java", "qa"], 2018, "Managed repositories, boards and release pipelines in {skills}.", month=9),
    "github actions": _k("DevOps & CI/CD", ["devops", "java", "python", "frontend"], 2019, "Automated build and test workflows with {skills}.", ["github ci/cd"], month=11),
    "gitlab ci": _k("DevOps & CI/CD", ["devops", "java", "python"], 2015, "Configured {skills} pipelines for build and deployment."),
    "git": _k("Version Control & Collaboration", [G], 2005, "Used {skills} branching and pull-request workflows for collaborative development.", ["git workflows", "git/github version control", "version control"]),
    "github": _k("Version Control & Collaboration", [G], 2008, "Managed code reviews and pull requests on {skills}."),
    "gitlab": _k("Version Control & Collaboration", [G], 2011, "Managed repositories and merge requests on {skills}."),
    "jira": _k("Version Control & Collaboration", [G], 2002, "Tracked user stories, tasks and defects in {skills}.", ["jira service desk"]),
    "linux": _k("Operating Systems & Virtualization", ["devops", "cloud", "network", "java", "python", "data"], 1991, "Administered and troubleshot {skills} servers for application deployments.", ["linux administration", "linux/unix administration", "unix command line"]),
    "rhel": _k("Operating Systems & Virtualization", ["devops", "cloud", "network"], 2002, "Administered {skills} servers, including patching and service configuration.", ["rhel/linux administration"]),
    "prometheus": _k("Testing & Monitoring", ["devops", "cloud"], 2015, "Monitored services with {skills} metrics and alerting rules."),
    "grafana": _k("Testing & Monitoring", ["devops", "cloud"], 2014, "Built {skills} dashboards for service health and capacity."),
    "splunk": _k("Testing & Monitoring", ["devops", "security", "cloud"], 2004, "Built {skills} searches and dashboards for log analysis and incident troubleshooting.", ["spl queries", "splunk enterprise security", "splunk es/core"]),
    "dynatrace": _k("Testing & Monitoring", ["devops", "cloud"], 2014, "Monitored application performance with {skills}."),
    "elk": _k("Testing & Monitoring", ["devops", "cloud"], 2012, "Centralized logs with the {skills} stack for troubleshooting."),
    "sonarqube": _k("Testing & Monitoring", ["devops", "java", "qa"], 2008, "Integrated {skills} code-quality scans into build pipelines.", ["snyk", "trivy", "checkov"]),
    "blue-green deployments": _k("DevOps & CI/CD", ["devops"], 2010, "Released changes with {skills} to minimise downtime.", ["rolling deployments"]),
    # data
    "snowflake": _k("Data Warehousing", ["data"], 2015, "Built {skills} tables, views and transformations for analytics workloads."),
    "databricks": _k("Data Engineering & Governance", ["data", "ml"], 2015, "Developed {skills} notebooks and jobs for large-scale data transformation.", ["azure databricks", "databricks lakehouse", "databricks lakehouse architecture"]),
    "spark": _k("Big Data & Streaming", ["data", "ml"], 2014, "Processed large datasets with {skills} transformations and optimized partitioning.", ["apache spark", "pyspark"]),
    "delta lake": _k("Data Engineering & Governance", ["data"], 2019, "Managed {skills} tables with schema enforcement and incremental merges.", month=4),
    "unity catalog": _k("Data Engineering & Governance", ["data"], 2022, "Governed data access and lineage with {skills}.", month=8),
    "airflow": _k("Big Data & Streaming", ["data", "ml"], 2015, "Orchestrated data pipelines with {skills} DAGs and scheduling.", ["apache airflow"]),
    "dbt": _k("Data Engineering & Governance", ["data"], 2016, "Built {skills} models with tests and documentation for the analytics layer."),
    "etl": _k("Data Engineering & Governance", ["data"], 1995, "Built {skills} workflows to extract, cleanse and load data into the warehouse.", ["etl/elt", "elt"]),
    "data modeling": _k("Data Engineering & Governance", ["data", "db"], 1990, "Designed relational and dimensional data models for reporting needs.", ["kimball dimensional modeling", "schema design", "database modeling"]),
    "hadoop": _k("Big Data & Streaming", ["data"], 2006, "Processed data on {skills} clusters."),
    "hive": _k("Big Data & Streaming", ["data"], 2010, "Queried large datasets with {skills}."),
    "bigquery": _k("Data Warehousing", ["data", "cloud"], 2011, "Wrote {skills} SQL for analytics datasets."),
    "redshift": _k("Data Warehousing", ["data", "cloud"], 2013, "Loaded and queried data in {skills}.", ["amazon redshift"]),
    "microsoft fabric": _k("Data Engineering & Governance", ["data"], 2023, "Built lakehouse items in {skills}.", month=11),
    "azure synapse": _k("Data Warehousing", ["data"], 2020, "Built {skills} pipelines and SQL pools.", month=12),
    "power bi": _k("Data Processing & Visualization", ["data", "bi"], 2015, "Built {skills} reports and dashboards with DAX measures.", ["power bi service"], month=7),
    "tableau": _k("Data Processing & Visualization", ["data", "bi"], 2003, "Built {skills} dashboards for business users."),
    "pandas": _k("Data Processing & Visualization", ["python", "data", "ml"], 2008, "Cleaned and transformed datasets with {skills}."),
    "numpy": _k("Data Processing & Visualization", ["python", "data", "ml"], 2006, "Performed numerical processing with {skills}."),
    "informatica": _k("Data Engineering & Governance", ["data"], 1993, "Built {skills} mappings and workflows."),
    # databases
    "postgresql": _k("Databases & Data Stores", ["db", "java", "python", "data"], 1996, "Designed {skills} schemas, indexes and queries for application data.", ["postgres"]),
    "oracle": _k("Databases & Data Stores", ["db", "java", "data"], 1979, "Worked with {skills} database schemas, queries and stored procedures.", ["oracle db", "oracle db server"]),
    "mysql": _k("Databases & Data Stores", ["db", "java", "python"], 1995, "Designed and queried {skills} schemas."),
    "sql server": _k("Databases & Data Stores", ["db", "dotnet", "data"], 1989, "Wrote T-SQL queries and procedures on {skills}.", ["ms sql server"]),
    "mongodb": _k("Databases & Data Stores", ["db", "java", "python", "frontend"], 2009, "Modeled documents and queries in {skills}."),
    "cassandra": _k("Databases & Data Stores", ["db", "java", "data"], 2008, "Designed {skills} tables for high-write workloads."),
    "redis": _k("Databases & Data Stores", ["db", "java", "python"], 2009, "Used {skills} caching to reduce database load."),
    "dynamodb": _k("Databases & Data Stores", ["db", "cloud", "python", "java"], 2012, "Modeled {skills} tables and access patterns.", ["aws dynamodb"]),
    "cosmos db": _k("Databases & Data Stores", ["db", "cloud", "dotnet"], 2017, "Modeled {skills} containers and queries.", month=5),
    "neo4j": _k("Databases & Data Stores", ["db", "data"], 2007, "Modeled graph data in {skills}."),
    "amazon neptune": _k("Databases & Data Stores", ["db", "data", "cloud"], 2018, "Modeled graph data in {skills}.", ["neptune"], month=5),
    # AI / GenAI
    "machine learning": _k("AI / ML & GenAI", ["ml", "data"], 1990, "Built and evaluated {skills} models.", ["ai/ml"]),
    "generative ai": _k("AI / ML & GenAI", ["ml"], 2022, "Built {skills} features using LLM APIs.", ["genai", "ai/generative ai", "genai applications", "llm applications", "llm integration"], month=11),
    "llm": _k("AI / ML & GenAI", ["ml"], 2020, "Integrated {skills} capabilities into application workflows.", ["llms"], month=6),
    "rag": _k("AI / ML & GenAI", ["ml"], 2020, "Built {skills} pipelines combining retrieval with LLM responses.", month=5),
    "langchain": _k("AI / ML & GenAI", ["ml"], 2022, "Built LLM workflows with {skills} chains and tools.", month=10),
    "langgraph": _k("AI / ML & GenAI", ["ml"], 2024, "Built stateful agent workflows with {skills}."),
    "llamaindex": _k("AI / ML & GenAI", ["ml"], 2022, "Built document indexing and retrieval with {skills}.", month=11),
    "crewai": _k("AI / ML & GenAI", ["ml"], 2024, "Built multi-agent workflows with {skills}."),
    "autogen": _k("AI / ML & GenAI", ["ml"], 2023, "Built multi-agent workflows with {skills}.", month=9),
    "ai agents": _k("AI / ML & GenAI", ["ml"], 2023, "Built {skills} with tool calling and orchestration.", ["agentic ai", "ai agents / agent development", "agent orchestration", "multi agent orchestration"], month=3),
    "model context protocol": _k("AI / ML & GenAI", ["ml"], 2024, "Exposed tools to LLM agents through {skills} servers.", ["model context protocols (mcps)", "mcp"], month=11),
    "prompt engineering": _k("AI / ML & GenAI", ["ml"], 2022, "Applied {skills} to improve LLM output quality."),
    "vector databases": _k("Vector Databases & Search", ["ml"], 2021, "Stored and searched embeddings in {skills}.", ["vector search"]),
    "embeddings": _k("AI / ML & GenAI", ["ml"], 2018, "Generated and indexed {skills} for semantic retrieval."),
    "aws bedrock": _k("AI / ML & GenAI", ["ml", "cloud"], 2023, "Integrated foundation models through {skills}.", month=9),
    "azure ai foundry": _k("AI / ML & GenAI", ["ml", "cloud"], 2024, "Built and deployed AI applications with {skills}.", month=11),
    "pytorch": _k("AI / ML & GenAI", ["ml"], 2016, "Trained models with {skills}."),
    "tensorflow": _k("AI / ML & GenAI", ["ml"], 2015, "Trained models with {skills}.", month=11),
    "nlp": _k("AI / ML & GenAI", ["ml"], 1990, "Built {skills} pipelines for text processing."),
    "github copilot": _k("AI-Assisted Development", [G], 2022, "Used {skills} to speed up coding and test writing.", month=6),
    "claude code": _k("AI-Assisted Development", [G], 2025, "Used {skills} for AI-assisted development.", month=2),
    "cursor": _k("AI-Assisted Development", [G], 2023, "Used {skills} for AI-assisted development.", month=3),
    "windsurf": _k("AI-Assisted Development", [G], 2024, "Used {skills} for AI-assisted development.", month=11),
    "copilot studio": _k("AI / ML & GenAI", ["m365", "ml"], 2023, "Built copilots in {skills}.", month=11),
    "microsoft 365 copilot": _k("AI / ML & GenAI", ["m365", "ml"], 2023, "Rolled out and supported {skills} for business users.", ["microsoft copilot"], month=11),
    # QA
    "selenium": _k("Testing & QA", ["qa"], 2004, "Automated UI regression tests with {skills} WebDriver."),
    "playwright": _k("Testing & QA", ["qa", "frontend"], 2020, "Automated end-to-end browser tests with {skills}."),
    "cypress": _k("Testing & QA", ["qa", "frontend"], 2017, "Automated end-to-end UI tests with {skills}."),
    "cucumber": _k("Testing & QA", ["qa"], 2008, "Wrote BDD scenarios with {skills} and Gherkin."),
    "rest assured": _k("Testing & QA", ["qa"], 2010, "Automated API tests with {skills}."),
    "postman": _k("Testing & QA", ["qa", "java", "python", "frontend"], 2014, "Tested and documented APIs with {skills} collections."),
    "tosca": _k("Testing & QA", ["qa"], 2008, "Automated test cases with {skills}.", ["tricentis tosca", "tosca automation for sap/successfactors"]),
    "tdd": _k("Methodologies & Practices", ["java", "python", "dotnet", "frontend", "qa"], 2003, "Applied {skills} by writing tests ahead of implementation."),
    # platforms
    "salesforce": _k("Enterprise Platforms", ["salesforce"], 2000, "Configured and customized {skills} objects, flows and security.", ["sales cloud", "salesforce integration", "sfdc"]),
    "apex": _k("Enterprise Platforms", ["salesforce"], 2007, "Developed {skills} classes and triggers with test coverage."),
    "servicenow": _k("Enterprise Platforms", ["servicenow"], 2004, "Configured {skills} workflows, forms and business rules.", ["servicenow itsm", "servicenow itom", "servicenow cmdb", "servicenow itam"]),
    "sap s/4hana": _k("Enterprise Platforms", ["sap"], 2015, "Worked on {skills} configuration and integration.", ["s/4hana"], month=2),
    "sap btp": _k("Enterprise Platforms", ["sap"], 2021, "Built extensions on {skills}."),
    "workday": _k("Enterprise Platforms", ["workday"], 2006, "Configured {skills} business processes and integrations.", ["workday hcm", "workday integrations", "workday studio"]),
    "microsoft 365": _k("Enterprise Platforms", ["m365"], 2011, "Administered {skills} tenant services.", ["exchange online", "sharepoint online", "microsoft teams"]),
    "mulesoft": _k("Enterprise Platforms", ["integration", "java", "salesforce"], 2006, "Built {skills} APIs and integration flows."),
    # security / identity / network
    "oauth": _k("Security & Identity", ["java", "python", "dotnet", "frontend", "security", "identity"], 2012, "Implemented {skills}-based authentication and token handling.", ["oauth 2.0", "oauth2"]),
    "openid connect": _k("Security & Identity", ["java", "python", "dotnet", "frontend", "security", "identity"], 2014, "Integrated {skills} sign-in flows.", ["oidc"]),
    "saml": _k("Security & Identity", ["security", "identity"], 2002, "Configured {skills} single sign-on integrations.", ["sso", "single sign-on (sso)"]),
    "iam": _k("Security & Identity", ["cloud", "devops", "security", "identity"], 2010, "Configured {skills} roles and least-privilege policies."),
    "rbac": _k("Security & Identity", ["cloud", "devops", "security", "identity", "java", "dotnet"], 2000, "Implemented {skills} so users only reach the functions their role allows.", ["rbac implementation", "role-based access management (rbac)"]),
    "entra id": _k("Security & Identity", ["identity", "m365", "cloud"], 2023, "Administered {skills} users, groups and app registrations.", ["azure ad", "microsoft entra id (azure ad)", "azure active directory (entra id)"], month=7),
    "owasp": _k("Security & Identity", ["security", "java", "frontend"], 2001, "Applied {skills} guidelines to remediate vulnerabilities."),
    "zscaler": _k("Security & Identity", ["security", "network"], 2008, "Supported {skills} access policies.", ["zscaler zero-trust"]),
    "dns": _k("Networking", ["network", "devops"], 1985, "Administered {skills} records and resolved name-resolution issues.", ["dns administration"]),
    "dhcp": _k("Networking", ["network", "devops"], 1993, "Managed {skills} scopes and reservations.", ["dhcp administration"]),
}

# ---------------------------------------------------------------------------
# Category keyword map for skills NOT in the KB (first match wins). Tried
# after claude_service.categorize_skills so existing rows are reused.
# ---------------------------------------------------------------------------
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("AI / ML & GenAI", ["ai", "llm", "genai", "gen ai", "agent", "agentic", "rag", "ml", "nlp", "copilot", "prompt", "embedding", "model", "conversational", "bot"]),
    ("Vector Databases & Search", ["vector", "semantic search", "opensearch", "elasticsearch"]),
    ("Cloud Platforms", ["azure", "aws", "gcp", "google cloud", "cloud", "amazon", "ec2", "s3", "vpc", "rds", "app service", "key vault", "front door", "cloudflare"]),
    ("DevOps & CI/CD", ["devops", "ci/cd", "pipeline", "deployment", "release", "container", "kubernetes", "openshift", "helm", "terraform", "ansible", "argo", "build"]),
    ("Data Engineering & Governance", ["data", "etl", "elt", "lakehouse", "lake", "warehouse", "lineage", "catalog", "mdm", "master data", "ingestion", "cdc", "schema", "medallion", "iceberg", "nifi", "informatica", "collibra", "alation", "purview"]),
    ("Databases & Data Stores", ["sql", "database", "db2", "oracle", "postgres", "mysql", "nosql", "query", "indexing", "sharding", "replication", "spanner", "yugabyte", "teradata", "sqlite"]),
    ("Data Processing & Visualization", ["power bi", "tableau", "dashboard", "dax", "power query", "obiee", "looker", "excel", "d3"]),
    ("Testing & QA", ["test", "testing", "qa", "selenium", "cucumber", "uat", "sit", "regression", "automation tester", "xctest", "xcuitest", "robot framework", "loadrunner", "jmeter", "defect"]),
    ("Testing & Monitoring", ["monitoring", "observability", "logging", "telemetry", "alert", "nagios", "zabbix", "solarwinds", "check mk", "apm", "splunk", "datadog", "new relic"]),
    ("Security & Identity", ["security", "secure", "iam", "identity", "auth", "sso", "saml", "mfa", "rbac", "zero trust", "zero-trust", "siem", "soc", "threat", "vulnerab", "firewall", "waf", "crowdstrike", "defender", "sentinel", "nessus", "tenable", "sast", "sca", "owasp", "encryption", "kerberos", "ldap", "conditional access", "pim", "scim", "imperva", "proofpoint", "wiz", "prisma", "secrets", "kms", "tls", "certificate"]),
    ("Networking", ["network", "networking", "lan", "wan", "sd-wan", "vpn", "dns", "dhcp", "tcp/ip", "routing", "switching", "cisco", "meraki", "ubiquiti", "opengear", "f5", "load balanc", "wireless", "ip address", "bgp", "fortinet", "palo alto", "hpe", "http", "cdn"]),
    ("Operating Systems & Virtualization", ["linux", "unix", "windows", "rhel", "centos", "ubuntu", "solaris", "aix", "hp-ux", "vmware", "vsphere", "vmotion", "hyper-v", "virtualization", "server", "patch"]),
    ("Mainframe", ["mainframe", "jcl", "cobol", "rexx", "tso", "ispf", "smp/e", "smpe", "ims", "zos", "z/os", "clist", "iebcopy", "iebgener", "dfdss"]),
    ("Mobile Development", ["ios", "android", "swift", "kotlin", "xcode", "uikit", "swiftui", "cocoapods", "react native", "flutter", "mobile", "app store", "play store", "retrofit", "volley", "room database", "core data", "keychain"]),
    ("Frontend Frameworks & Tools", ["react", "angular", "vue", "next.js", "remix", "svelte", "webpack", "vite", "babel", "storybook", "css", "html", "frontend", "front-end", "ui", "ux", "wcag", "accessibility", "aria", "responsive", "micro-frontend", "module federation", "npm", "yarn", "pnpm", "redux", "state management"]),
    ("Enterprise Platforms", ["sap", "salesforce", "servicenow", "workday", "oracle cloud", "oracle erp", "oracle epm", "dynamics", "veeva", "guidewire", "actimize", "loan iq", "flexcube", "aem", "adobe", "sharepoint", "microsoft 365", "m365", "power platform", "power apps", "power automate", "mulesoft", "ibm mq", "mft", "sterling", "goanywhere", "axway", "globalscape", "edi", "x12", "edifact", "as2", "sftp", "ftps", "cloudera", "fiori", "abap", "idoc", "bapi"]),
    ("Backend Frameworks & APIs", ["api", "rest", "soap", "graphql", "grpc", "microservice", "spring", "hibernate", "jpa", "jdbc", "j2ee", "jboss", "middleware", "webhook", "odata", "event", "messaging", "service bus", "event hub", "integration", "backend", "back-end", "dependency injection", "multi-threading", "asynchronous", "reactive", "caching", "design pattern", "architecture"]),
    ("Programming Languages", ["java", "python", "c#", "c++", "go", "scala", "javascript", "typescript", "objective-c", "groovy", "bash", "shell", "powershell", "script", "pro c", "asp"]),
    ("Version Control & Collaboration", ["git", "github", "gitlab", "jira", "confluence", "version control", "bitbucket", "svn"]),
]

PRIORITY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Networking", ["vnet", "vnets", "vpc", "subnet", "peering", "nsg", "nsgs", "private endpoint", "private endpoints",
                    "application gateway", "front door", "load balancer", "load balancers", "expressroute", "direct connect",
                    "cloud networking", "vpn", "dns", "firewall"]),
    ("Reliability & Operations", ["disaster recovery", "high availability", "ha/dr", "fault toler", "failover", "backup",
                                  "capacity planning", "self-healing", "site recovery", "business continuity", "sla",
                                  "performance", "scalab", "distributed", "platform upgrades", "infrastructure", "it infrastructure",
                                  "incident", "triage", "patch", "upgrade", "migration", "consolidation"]),
    ("Security & Identity", ["authentication", "authorization", "devsecops", "compliance", "incident response", "access control",
                             "data privacy", "privacy", "encryption", "https", "zero trust"]),
    ("Databases & Data Stores", ["databases", "relational", "non-relational", "transaction management", "acid"]),
    ("Mobile Development", ["mvvm", "viper", "gcd", "combine", "userdefaults", "instruments", "firebase", "core data", "keychain"]),
    ("Frontend Frameworks & Tools", ["bootstrap", "bootstrapjs", "jsf", "package managers", "full stack"]),
    ("Backend Frameworks & APIs", ["xslt", "log4j", "eclipse", "sts", "design patterns", "enterprise design", "service design", "software engineering", "engineering design", "system implementation", "technical specifications"]),
    ("Big Data & Streaming", ["streaming", "avro", "parquet", "real-time"]),
    ("Data Engineering & Governance", ["onelake", "fabric", "metadata", "lineage", "catalog"]),
    ("Cloud Platforms", ["logic apps", "avd", "azure virtual desktop", "openstack", "cloud foundry", "digitalocean"]),
    ("Enterprise Platforms", ["workflow", "ibm connect", "globalscape", "xlr", "ctr module", "actimize"]),
    ("Security & Identity", ["key vault", "secrets", "secret manager", "kms", "defender", "sentinel", "policy", "conditional access"]),
    ("Testing & Monitoring", ["log analytics", "monitor", "monitoring", "observability", "application insights"]),
]

# Words / phrases that are methodologies, soft skills or delivery practices.
PRACTICES = {
    "agile", "scrum", "agile/scrum", "kanban", "safe", "waterfall", "sprint planning", "sprint reviews", "retrospectives",
    "daily stand-ups", "release planning", "risk management", "budget management", "project management", "program management",
    "stakeholder management", "stakeholder communication", "stakeholder management & leadership", "troubleshooting",
    "communication skills", "communication and collaboration", "executive communication", "business & it communication",
    "requirements gathering", "requirement gathering", "requirements elicitation", "requirements execution",
    "change management", "digital transformation", "status reporting", "resource planning", "product backlog management",
    "cross-functional team management", "client management", "analytical and problem-solving", "code reviews",
    "rapid prototyping", "production support", "problem management", "incident management", "root-cause analysis", "rca",
    "impact analysis", "business analysis", "business process analysis", "business requirements analysis",
    "business systems analysis", "test planning", "test strategy", "test plans", "test management", "defect management",
    "sdlc", "stlc", "scrum ceremonies", "project planning", "pmo governance", "hybrid delivery", "solution design",
    "system design", "design patterns", "solid principles", "object-oriented programming", "clean architecture",
    "tdd", "itil processes", "itil", "itil v3/v4", "release management", "release engineering", "configuration management",
    "project management methodologies", "it project management", "supply chain project management", "recruiting",
    "scrum master", "product development", "product development (pd)", "supplier management", "financial management",
    "it asset lifecycle management", "solution design & implementation", "open-source", "mentoring",
}
DOMAIN_HINTS = [
    "insurance", "claims", "policy", "banking", "wealth", "investment", "payment", "card acquiring", "aml", "fin-crime",
    "financial crimes", "fincen", "ctr", "telecom", "healthcare", "clinical", "patient", "pharma", "life sciences",
    "medtech", "hipaa", "fhir", "hl7", "retail", "supply chain", "procurement", "sourcing", "payroll", "compensation",
    "talent", "value-based care", "population health", "property and auto", "p&c", "sox", "soc 2", "nist", "cmmc",
    "cis", "emr", "commercial data", "sales data", "customer 360", "rating", "exemption", "territory", "parent training",
    "treatment plan", "rbt", "behavior", "hcp/hco", "promomats", "materials planning", "loan",
]

# Certifications — never claimed (kept in missing_skills).
CERT_RE = re.compile(
    r"\b(pmp|csm|pmi-acp|prince2|oscp|cissp|cism|cisa|ccna|ccnp|bcba|az[ -]?\d{3}|safe sm|safe scrum master|"
    r"certification|certified)\b|\b(cysa|security|network|a|comptia)\+(?![a-z0-9])", re.I)
_CERT_SUFFIX_RE = re.compile(r"^(.*?)\s*\(([^)]*certif[^)]*)\)\s*$", re.I)

# Parser placeholders that are not skills.
JUNK = {"developer", "admin", "administration", "support", "configuration", "deployment", "infrastructure", "mentor architects", "-", ""}

# ---------------------------------------------------------------------------
# Sentence templates per category, used for bullets grouping 2+ skills
# (single-skill bullets use the KB template when there is one).
# ---------------------------------------------------------------------------
CATEGORY_TEMPLATES = {
    "Programming Languages": "Developed application and automation code using {skills}.",
    "Backend Frameworks & APIs": "Built and integrated backend services and APIs using {skills}.",
    "Frontend Frameworks & Tools": "Built responsive UI features using {skills}.",
    "Cloud Platforms": "Deployed and supported application workloads using {skills}.",
    "DevOps & CI/CD": "Automated build, release and infrastructure workflows using {skills}.",
    "Big Data & Streaming": "Processed batch and streaming data using {skills}.",
    "Data Engineering & Governance": "Built and maintained data pipelines, models and governance processes using {skills}.",
    "Data Warehousing": "Loaded and modeled analytics data using {skills}.",
    "Databases & Data Stores": "Designed, queried and tuned databases using {skills}.",
    "Data Processing & Visualization": "Prepared data and built reports using {skills}.",
    "AI / ML & GenAI": "Built AI/ML and GenAI features using {skills}.",
    "Vector Databases & Search": "Implemented semantic search using {skills}.",
    "AI-Assisted Development": "Used {skills} to accelerate development and testing.",
    "Testing & QA": "Planned and automated testing using {skills}.",
    "Testing & Monitoring": "Strengthened quality and observability using {skills}.",
    "Version Control & Collaboration": "Collaborated on code and delivery using {skills}.",
    "Web Technologies": "Built web pages and data exchange formats using {skills}.",
    "Security & Identity": "Applied security and identity controls using {skills}.",
    "Networking": "Configured and supported network infrastructure including {skills}.",
    "Operating Systems & Virtualization": "Administered and supported environments running {skills}.",
    "Enterprise Platforms": "Configured and supported enterprise platform capabilities including {skills}.",
    "Mainframe": "Worked on mainframe components including {skills}.",
    "Reliability & Operations": "Improved platform reliability through {skills} planning and implementation.",
    "Mobile Development": "Built mobile app features using {skills}.",
    "Methodologies & Practices": "Followed {skills} practices throughout project delivery.",
    "Domain Knowledge": "Supported business processes related to {skills}.",
    "Other Tools & Technologies": "Worked with {skills} as part of project delivery.",
}
SINGLE_TEMPLATES = {
    "Methodologies & Practices": "Applied {skills} throughout project delivery.",
    "Domain Knowledge": "Supported business processes related to {skills}.",
}
NON_TECH_CATEGORIES = {"Methodologies & Practices", "Domain Knowledge"}

MAX_NEW_BULLETS = 3            # new work-experience points per generated resume
MAX_BULLETS_PER_PROJECT = 2    # ... in either of the two most recent projects
MAX_SKILLS_PER_BULLET = 4

SUMMARY_PURPOSE = {
    "Programming Languages": "application development", "Backend Frameworks & APIs": "backend services and APIs",
    "Frontend Frameworks & Tools": "responsive user interfaces", "Cloud Platforms": "cloud workloads",
    "DevOps & CI/CD": "container platforms and release automation", "Big Data & Streaming": "batch and streaming data processing",
    "Data Engineering & Governance": "data pipelines and governance", "Data Warehousing": "analytics data platforms",
    "Databases & Data Stores": "data storage and query performance", "Data Processing & Visualization": "reporting and analytics",
    "AI / ML & GenAI": "AI/ML and GenAI solutions", "Vector Databases & Search": "semantic search",
    "AI-Assisted Development": "faster development and testing", "Testing & QA": "test automation",
    "Testing & Monitoring": "monitoring and observability", "Version Control & Collaboration": "team collaboration",
    "Web Technologies": "web development", "Security & Identity": "secure access control",
    "Networking": "cloud networking", "Operating Systems & Virtualization": "system administration",
    "Enterprise Platforms": "enterprise platform delivery", "Mainframe": "mainframe operations",
    "Mobile Development": "mobile app development", "Reliability & Operations": "platform reliability",
    "Methodologies & Practices": "project delivery", "Domain Knowledge": "business process support",
    "Other Tools & Technologies": "project delivery",
}

CATEGORY_DOMAINS = {
    "Programming Languages": {G}, "Backend Frameworks & APIs": {"java", "python", "dotnet", "frontend"},
    "Frontend Frameworks & Tools": {"frontend"}, "Cloud Platforms": {"cloud", "devops", "data"},
    "DevOps & CI/CD": {"devops", "cloud"}, "Big Data & Streaming": {"data"}, "Data Engineering & Governance": {"data"},
    "Data Warehousing": {"data"}, "Databases & Data Stores": {"db", "data", "java", "python", "dotnet"},
    "Data Processing & Visualization": {"data", "bi"}, "AI / ML & GenAI": {"ml"}, "Vector Databases & Search": {"ml"},
    "AI-Assisted Development": {G}, "Testing & QA": {"qa"}, "Testing & Monitoring": {"devops", "cloud", "qa"},
    "Version Control & Collaboration": {G}, "Web Technologies": {"frontend"}, "Security & Identity": {"security", "identity", "devops", "cloud"},
    "Networking": {"network", "devops"}, "Operating Systems & Virtualization": {"devops", "cloud", "network"},
    "Enterprise Platforms": {"salesforce", "sap", "servicenow", "workday", "m365", "integration"}, "Mainframe": {"mainframe"},
    "Mobile Development": {"mobile"}, "Reliability & Operations": {"devops", "cloud", "db"}, "Methodologies & Practices": {G}, "Domain Knowledge": {G}, "Other Tools & Technologies": {G},
}

ROLE_DOMAIN_RULES = [
    (r"devops|infra|sre|site reliability|platform|cloud|kubernetes|release|system", {"devops", "cloud"}),
    (r"python", {"python"}),
    (r"\bjava\b|j2ee|jee|spring", {"java"}),
    (r"\.net|dotnet|c#", {"dotnet"}),
    (r"full ?stack|front|ui\b|react|angular", {"frontend"}),
    (r"data|etl|bi\b|analytics|warehouse|databricks|snowflake", {"data"}),
    (r"\bai\b|ml\b|machine learning|gen ?ai|data scientist|llm", {"ml", "python"}),
    (r"sdet|qa\b|test|quality", {"qa"}),
    (r"salesforce", {"salesforce"}), (r"\bsap\b", {"sap"}), (r"servicenow", {"servicenow"}), (r"workday", {"workday"}),
    (r"network", {"network"}), (r"secur|soc\b|iam\b|identity", {"security", "identity"}),
    (r"mobile|ios|android", {"mobile"}), (r"mainframe", {"mainframe"}), (r"admin", {"devops"}),
    (r"database|dba", {"db"}), (r"m365|microsoft 365|office 365", {"m365"}),
]

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def join_natural(items: list[str]) -> str:
    """'A' / 'A and B' / 'A, B and C'. Must match joinNatural() in the frontend."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def render(template: str, skills: list[str]) -> str:
    return template.replace("{skills}", join_natural(skills))


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


_SYNONYMS = [
    {"kubernetes", "k8s"}, {"go", "golang"}, {"javascript", "js"}, {"react", "reactjs", "react.js", "react js"},
    {"node.js", "nodejs", "node js", "node"}, {"postgresql", "postgres"}, {"gcp", "google cloud", "google cloud platform"},
    {"aws", "amazon web services"}, {"azure", "microsoft azure"}, {"ci/cd", "cicd", "ci/cd pipelines", "ci/cd pipeline"},
    {"entra id", "azure ad", "azure active directory", "microsoft entra id"}, {"salesforce", "sfdc"},
    {"sql server", "ms sql server", "mssql"}, {"spring boot", "springboot"}, {"rest api", "rest apis", "restful api", "restful apis", "rest"},
    {"c#", "csharp"}, {".net", "dotnet"}, {"next.js", "nextjs"}, {"vue.js", "vue", "vuejs"}, {"html", "html5"}, {"css", "css3"},
    {"azure kubernetes service", "aks"}, {"aws eks", "eks"}, {"azure data factory", "adf"}, {"power bi", "powerbi"},
    {"github actions", "gh actions"}, {"infrastructure as code", "iac"}, {"oauth", "oauth2", "oauth 2.0"},
    {"openid connect", "oidc"}, {"machine learning", "ml"}, {"generative ai", "genai", "gen ai"}, {"llm", "llms"},
    {"model context protocol", "mcp", "model context protocols"}, {"spark", "apache spark"}, {"kafka", "apache kafka"},
    {"airflow", "apache airflow"}, {"rhel", "red hat enterprise linux"}, {"openshift", "red hat openshift"},
    {"shell scripting", "bash scripting"},
]
_SYN_INDEX = {}
for _i, _grp in enumerate(_SYNONYMS):
    for _w in _grp:
        _SYN_INDEX[_w] = _i


def skill_key(s: Any) -> str:
    """Comparable key for 'is this the SAME skill' (not 'related')."""
    n = _norm(s)
    n = re.sub(r"\s*\((?:[^)]*)\)\s*", " ", n).strip()           # "Azure Data Factory (ADF)" -> "azure data factory"
    n = re.sub(r"^(apache|microsoft|amazon|google)\s+", "", n) if _norm(s) not in _SYN_INDEX else n
    n = n.replace("–", "-")
    n = re.sub(r"\s+", " ", n).strip(" .,;:")
    if n in _SYN_INDEX:
        return f"syn{_SYN_INDEX[n]}"
    if _norm(s) in _SYN_INDEX:
        return f"syn{_SYN_INDEX[_norm(s)]}"
    nn = re.sub(r"[^a-z0-9#+./]", "", n)
    if nn.endswith("s") and len(nn) > 3 and not nn.endswith(("ss", "us", "is", "js", "os")):
        nn = nn[:-1]
    return nn


def _alias_index() -> dict[str, str]:
    idx = {}
    for canon, e in KB.items():
        idx.setdefault(_norm(canon), canon)
        for a in e["aliases"]:
            idx.setdefault(_norm(a), canon)
    return idx


_ALIAS = _alias_index()


def kb_lookup(s: str) -> Optional[str]:
    n = _norm(s)
    if n in _ALIAS:
        return _ALIAS[n]
    n2 = re.sub(r"\s*\(.*?\)\s*", " ", n).strip()
    if n2 in _ALIAS:
        return _ALIAS[n2]
    for pre in ("apache ", "microsoft ", "amazon "):
        if n2.startswith(pre) and n2[len(pre):] in _ALIAS:
            return _ALIAS[n2[len(pre):]]
    return None


@lru_cache(maxsize=8192)
def _word_re(phrase: str) -> "re.Pattern[str]":
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])")


def _mentions(text_lower: str, phrase: str) -> bool:
    p = _norm(phrase)
    if not p or p not in text_lower:
        return False
    return _word_re(p).search(text_lower) is not None


def _kw_hit(text_lower: str, kw: str) -> bool:
    if kw not in text_lower:
        return False
    if kw.endswith(("vulnerab", "load balanc", "secur", "fault toler", "scalab")):
        return True
    return _word_re(kw).search(text_lower) is not None


def classify(skill: str) -> str:
    """cert | junk | practice | domain | tech"""
    n = _norm(skill)
    if n in JUNK:
        return "junk"
    if CERT_RE.search(n):
        return "cert"
    if kb_lookup(skill):
        return "tech"
    if n in PRACTICES:
        return "practice"
    if any(_kw_hit(n, d) for d in DOMAIN_HINTS) and not any(_kw_hit(n, k) for k in ("data", "api", "integration", "module", "testing", "logic", "system")):
        return "domain"
    return "tech"


def category_for(skill: str, kind: str, existing_categorizer=None) -> str:
    if kind == "practice":
        return "Methodologies & Practices"
    if kind == "domain":
        return "Domain Knowledge"
    canon = kb_lookup(skill)
    if canon:
        return KB[canon]["cat"]
    n = _norm(skill)
    for cat, kws in PRIORITY_KEYWORDS:
        if any(_kw_hit(n, k) for k in kws):
            return cat
    if existing_categorizer is not None:
        try:
            cat = existing_categorizer([skill])[0]["category"]
            if cat and cat != "Other Tools & Technologies":
                return cat
        except Exception:
            pass
    for cat, kws in CATEGORY_KEYWORDS:
        if any(_kw_hit(n, k) for k in kws):
            return cat
    return "Other Tools & Technologies"


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_ym(s: Any, today: date, is_end: bool = False) -> Optional[tuple[int, int]]:
    """'Nov 2024', 'November 2024', '2024-11', '11/2024', '2024', 'Present' -> (y, m)."""
    t = _norm(s)
    if not t:
        return (today.year, today.month) if is_end else None
    if any(w in t for w in ("present", "current", "till date", "now", "ongoing", "date")):
        return (today.year, today.month)
    m = re.search(r"([a-z]{3})[a-z]*\.?\s*[-/' ]?\s*(\d{4})", t)
    if m and m.group(1) in _MONTHS:
        return (int(m.group(2)), _MONTHS[m.group(1)])
    m = re.search(r"(\d{4})[-/.](\d{1,2})", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d{1,2})[-/.](\d{4})", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return (int(m.group(2)), int(m.group(1)))
    m = re.search(r"(\d{4})", t)
    if m:
        return (int(m.group(1)), 12 if is_end else 1)
    return None


def _project_domains(exp: dict) -> set[str]:
    role = _norm(exp.get("role") or exp.get("title") or "")
    d: set[str] = set()
    for pat, doms in ROLE_DOMAIN_RULES:
        if re.search(pat, role):
            d |= doms
    text = _norm(" ".join(str(b) for b in (exp.get("bullets") or [])))
    for canon, e in KB.items():
        if len(e["domains"]) <= 2 and G not in e["domains"] and _mentions(text, canon):
            d |= e["domains"]
    return d


def _skill_domains(skill: str, category: str) -> set[str]:
    canon = kb_lookup(skill)
    if canon:
        return KB[canon]["domains"]
    return CATEGORY_DOMAINS.get(category, {G})


def _release(skill: str) -> Optional[tuple[int, int]]:
    canon = kb_lookup(skill)
    return KB[canon]["rel"] if canon else None


def _table_skill_names(row: dict) -> list[str]:
    v = row.get("skills")
    if isinstance(v, list):
        return [(x.get("name", "") if isinstance(x, dict) else str(x)).strip() for x in v]
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def augment_resume_with_jd_skills(
    resume_data: dict,
    jd_skills: Iterable[str],
    *,
    today: Optional[date] = None,
    existing_categorizer=None,
) -> dict:
    """Mutates and returns resume_data. Safe to call more than once."""
    today = today or date.today()
    if not isinstance(resume_data, dict):
        return resume_data
    if resume_data.get("jd_added"):
        return resume_data  # already augmented

    # ---- de-duplicate the requested skills (same-skill key), keep JD order
    requested: list[str] = []
    seen_keys: set[str] = set()
    cert_notes: list[str] = []
    for raw in jd_skills or []:
        s = re.sub(r"\s+", " ", str(raw or "")).strip().strip(",;")
        if not s:
            continue
        m = _CERT_SUFFIX_RE.match(s)
        if m and m.group(1).strip() and not CERT_RE.search(m.group(1)):
            # "Tricentis Tosca (Certification - MUST)": add the skill, keep the certificate as a gap
            cert_notes.append(f"{m.group(1).strip()} certification")
            s = m.group(1).strip()
        k = skill_key(s)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        requested.append(s)

    table = resume_data.get("technical_proficiencies")
    if not isinstance(table, list):
        table = []
    for row in table:  # normalise comma-string rows to lists
        if isinstance(row, dict) and not isinstance(row.get("skills"), list):
            row["skills"] = _table_skill_names(row)
    flat = resume_data.get("skills")
    if not isinstance(flat, list):
        flat = [s.strip() for s in str(flat or "").split(",") if s.strip()]

    have_keys = {skill_key(s) for row in table if isinstance(row, dict) for s in _table_skill_names(row)}
    have_keys |= {skill_key(s) for s in flat}

    experience = resume_data.get("experience") if isinstance(resume_data.get("experience"), list) else []
    all_bullets_lower = _norm(" ".join(_strip_tags(str(b)) for e in experience if isinstance(e, dict) for b in (e.get("bullets") or [])))
    summary_text = resume_data.get("summary") or resume_data.get("career_objective") or ""
    summary_lower = _norm(_strip_tags(summary_text))

    # two most recent projects
    dated = []
    for i, e in enumerate(experience):
        if not isinstance(e, dict):
            continue
        end = parse_ym(e.get("end"), today, is_end=True) or (today.year, today.month)
        start = parse_ym(e.get("start"), today) or end
        dated.append((end, start, i))
    dated.sort(key=lambda t: (t[0], t[1]), reverse=True)
    recent = dated[:2]
    proj_domains = {i: _project_domains(experience[i]) for _, _, i in recent}

    added_skills, skipped, no_bullet = [], [], []
    candidates: list[tuple[str, str]] = []      # (category, skill) new to the work experience, JD order

    for s in requested:
        kind = classify(s)
        if kind == "cert":
            skipped.append({"name": s, "reason": "certification"}); continue
        if kind == "junk":
            skipped.append({"name": s, "reason": "not a skill"}); continue
        k = skill_key(s)
        cat = category_for(s, kind, existing_categorizer)
        if k not in have_keys:                    # 1) EVERY skill goes into the skill table
            added_skills.append({"name": s, "category": cat})
            have_keys.add(k)
        if _mentions(all_bullets_lower, s):       # already shown in their own work experience
            continue
        candidates.append((cat, s))

    # ---- write skill table + flat list
    by_cat = {row.get("category"): row for row in table if isinstance(row, dict)}
    uses_dicts = any(isinstance(x, dict) for row in table if isinstance(row, dict) for x in (row.get("skills") or []))
    for a in added_skills:
        entry = {"name": a["name"], "isPrimary": False} if uses_dicts else a["name"]
        row = by_cat.get(a["category"])
        if row is None:
            row = {"category": a["category"], "skills": []}
            table.append(row)
            by_cat[a["category"]] = row
        row["skills"].append(entry)
        flat.append(a["name"])
    resume_data["technical_proficiencies"] = table
    resume_data["skills"] = flat

    # ---- 2) WORK EXPERIENCE: at most MAX_NEW_BULLETS new points in the two most
    # recent projects (MAX_BULLETS_PER_PROJECT each). One point = one category,
    # up to MAX_SKILLS_PER_BULLET skills. Biggest groups first (ties: JD order).
    groups: list[list] = []
    for cat, s in candidates:
        g = next((x for x in groups if x[0] == cat), None)
        if g is None:
            g = [cat, []]
            groups.append(g)
        g[1].append(s)
    ranked = sorted(enumerate(groups), key=lambda t: (-len(t[1][1]), t[0]))
    per_project: dict[int, int] = {}
    added_bullets = []
    for _, (cat, skills) in ranked:
        if len(added_bullets) >= MAX_NEW_BULLETS or not recent:
            break
        best = None
        for end, start, i in recent:
            if per_project.get(i, 0) >= MAX_BULLETS_PER_PROJECT:
                continue
            ok = [x for x in skills if _release(x) is None or end >= _release(x)][:MAX_SKILLS_PER_BULLET]
            if not ok:
                continue
            fits = any(G in _skill_domains(x, cat) or (_skill_domains(x, cat) & proj_domains[i]) for x in ok)
            score = (fits, len(ok))
            if best is None or score > best[0]:
                best = (score, i, ok)
        if best is None:
            for x in skills:
                rel = _release(x)
                if rel:
                    no_bullet.append({"name": x, "reason": f"released {rel[1]:02d}/{rel[0]} — after both recent projects ended"})
            continue
        _, exp_i, chunk = best
        if len(chunk) == 1:
            canon = kb_lookup(chunk[0])
            tpl = KB[canon]["t"] if canon else SINGLE_TEMPLATES.get(cat, CATEGORY_TEMPLATES.get(cat, CATEGORY_TEMPLATES["Other Tools & Technologies"]))
            if "{skills}" not in tpl:
                tpl = CATEGORY_TEMPLATES.get(cat, CATEGORY_TEMPLATES["Other Tools & Technologies"])
        else:
            tpl = CATEGORY_TEMPLATES.get(cat, CATEGORY_TEMPLATES["Other Tools & Technologies"])
        text = render(tpl, chunk)
        exp = experience[exp_i]
        if not isinstance(exp.get("bullets"), list):
            exp["bullets"] = [b for b in str(exp.get("bullets") or "").split("\n") if b.strip()]
        exp["bullets"].append(text)
        per_project[exp_i] = per_project.get(exp_i, 0) + 1
        added_bullets.append({"exp_index": exp_i, "category": cat, "template": tpl, "skills": chunk, "text": text})

    # ---- 3) PROFESSIONAL SUMMARY: 1-2 sentences/points naming the skills the new
    # points introduced, written in the SAME form as the base summary
    # (paragraph -> sentences joined into the paragraph; bullets -> bullets).
    summary_sources: list[tuple[list[str], list[str]]] = []   # (skills, categories)
    if added_bullets:
        b0 = added_bullets[0]
        summary_sources.append(([x for x in b0["skills"] if not _mentions(summary_lower, x)], [b0["category"]]))
        rest_sk, rest_cat = [], []
        for b in added_bullets[1:]:
            rest_sk += [x for x in b["skills"] if not _mentions(summary_lower, x)]
            rest_cat.append(b["category"])
        summary_sources.append((rest_sk, rest_cat))
    elif candidates and not recent:                       # no work experience to add points to
        top = ranked[0][1] if ranked else None
        if top:
            summary_sources.append(([x for x in top[1][:MAX_SKILLS_PER_BULLET] if not _mentions(summary_lower, x)], [top[0]]))
    added_summary = []
    for idx, (sk, cats) in enumerate(summary_sources):
        if not sk:
            continue
        purposes = list(dict.fromkeys(SUMMARY_PURPOSE.get(c, "project delivery") for c in cats))
        purpose = purposes[0] if len(purposes) == 1 else f"{purposes[0]}, as well as {purposes[1]}"
        lead = "Hands-on experience with" if not added_summary else "Also skilled in"
        tpl = f"{lead} {{skills}} for {purpose}."
        added_summary.append({"template": tpl, "skills": sk, "text": render(tpl, sk)})
    style = summary_style(summary_text)
    for p in added_summary:
        p["style"] = style
    if added_summary:
        new_summary = append_summary(summary_text, [p["text"] for p in added_summary])
        resume_data["summary"] = new_summary
        if resume_data.get("career_objective"):
            resume_data["career_objective"] = new_summary

    added_names = {skill_key(x) for b in added_bullets for x in b["skills"]}
    table_only = [a["name"] for a in added_skills if skill_key(a["name"]) not in added_names]

    # Skills Gap box keeps only what was deliberately not added.
    resume_data["missing_skills"] = [x["name"] for x in skipped if x["reason"] == "certification"] + cert_notes
    resume_data["jd_added"] = {
        "skills": added_skills,
        "bullets": added_bullets,
        "summary": added_summary,
        "table_only": table_only,
        "no_bullet": no_bullet,
        "skipped": skipped,
    }
    return resume_data


_BULLET_LINE = re.compile(r"^\s*([•▪●◦\-\*–])\s+")


def summary_style(summary: str) -> str:
    """'bullet' when the base summary is a list (HTML <li> or 2+ marker lines), else 'paragraph'."""
    s = summary or ""
    if re.search(r"<li\b", s, re.I):
        return "bullet"
    lines = [ln for ln in _strip_tags(s.replace("<br>", "\n")).split("\n") if ln.strip()]
    if sum(1 for ln in lines if _BULLET_LINE.match(ln)) >= 2:
        return "bullet"
    return "paragraph"


def append_summary(summary: str, sentences: list[str]) -> str:
    """Adds the sentences in the base summary's own form; base text untouched."""
    from html import escape
    s = (summary or "").rstrip()
    is_html = bool(re.search(r"<(p|ul|ol|li|br|strong|b|em|i|div|span)\b", s, re.I))
    if summary_style(s) == "bullet":
        if is_html:
            items = "".join(f"<li>{escape(x, quote=False)}</li>" for x in sentences)
            m = list(re.finditer(r"</(ul|ol)>", s, re.I))
            if m:
                pos = m[-1].start()
                return s[:pos] + items + s[pos:]
            return s + f"<ul>{items}</ul>"
        marker = next((_BULLET_LINE.match(ln).group(1) for ln in s.split("\n") if _BULLET_LINE.match(ln)), "•")
        return s + "".join(f"\n{marker} {x}" for x in sentences)
    joined = " ".join(sentences)
    if not s:
        return joined
    if is_html:
        m = list(re.finditer(r"</p>", s, re.I))
        if m:
            pos = m[-1].start()
            before = s[:pos].rstrip()
            sep = "" if before.endswith((".", "!", "?", ">")) else "."
            return before + sep + " " + escape(joined, quote=False) + s[pos:]
        return s + " " + escape(joined, quote=False)
    sep = "" if s.endswith((".", "!", "?")) else "."
    return s + sep + " " + joined


def strip_jd_added(resume_data: dict) -> dict:
    if isinstance(resume_data, dict):
        resume_data.pop("jd_added", None)
    return resume_data


# Aliases too ambiguous to trust in free JD text ("go-live", "the rest of", "spring release").
_TEXT_SKIP = {"go", "rest", "apis", "spring", "ml", "js", "es6", "s3", "lambda", "sso", "iac", "web services",
              "version control", "restful", "dotnet", "elt", "jpa", "adf", "aks", "eks", "mcp", "chef", "puppet",
              "cursor", "windsurf", "hive", "oracle", "tableau", "apex", "elk", "dns", "dhcp"}


def detect_jd_skills(job_description: str) -> list[str]:
    """Fallback when the requirement's parsed skill list isn't available:
    find known skills (KB names + aliases) in the JD text, in order of
    first appearance, using the JD's own spelling."""
    text = job_description or ""
    low = text.lower()
    hits: list[tuple[int, str, str]] = []
    for canon, e in KB.items():
        best = None
        for phrase in [canon] + e["aliases"]:
            p = _norm(phrase)
            if not p or p in _TEXT_SKIP or p not in low:
                continue
            m = _word_re(p).search(low)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), text[m.start():m.end()])
        if best:
            hits.append((best[0], canon, best[1]))
    hits.sort()
    out, seen = [], set()
    for _, canon, shown in hits:
        if canon not in seen:
            seen.add(canon)
            out.append(shown)
    return out