client = JiraInfrarchClient(
    base_url="https://jira.konzern.de",  # Eure echte Jira-URL
    pat_token="Dein_Echtes_Jira_PAT_Token",
    dry_run=False  # Live-Modus aktivieren!
)



python jira_bulk_creator_poc.py --live
