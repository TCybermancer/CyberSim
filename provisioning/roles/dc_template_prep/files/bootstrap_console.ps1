# One-time, run ONCE from an interactive console/RDP session (ESXi web
# console is fine) as a local administrator, elevated ("Run as
# Administrator"). This step exists only because Windows filters the
# admin token for any non-console (headless/remote) admin action taken
# by a local account other than the built-in RID-500 Administrator --
# by design, not a bug -- so it can't be done via VMware guest-ops or
# WinRM before WinRM itself exists. Everything after this is scripted.
#
# What this does:
#   1. Un-filters this account's token for future remote/headless admin
#      actions (guest-ops, WinRM), so roles/dc_template_prep's remaining
#      steps (unjoin domain, sysprep, convert to template) can run
#      unattended from here on.
#   2. Enables WinRM over HTTP (lab-only; see README for upgrading to an
#      HTTPS listener before this touches anything less trusted).

Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' `
    -Name LocalAccountTokenFilterPolicy -Value 1 -Type DWord -Force

winrm quickconfig -quiet -force
Set-Item -Path WSMan:\localhost\Service\Auth\Basic -Value $false
Set-Item -Path WSMan:\localhost\Service\Auth\Negotiate -Value $true
Set-Item -Path WSMan:\localhost\Service\AllowUnencrypted -Value $false
New-NetFirewallRule -DisplayName "WinRM-HTTP-Ansible" -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -ErrorAction SilentlyContinue

Write-Host "Done. LocalAccountTokenFilterPolicy=1 and WinRM are set -- Ansible/guest-ops can take it from here." -ForegroundColor Green
