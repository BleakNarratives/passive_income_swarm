#!/bin/bash
gh search issues "local LLM android" --json body > ~/passive_income_swarm/leads/hot_leads.txt
echo "$(date): Leads scanned." >> ~/passive_income_swarm/leads/lead_log.txt
