#!/usr/bin/env bash
# Crée le raccourci « Traitement SPED » sur le Bureau Windows.
# À lancer une seule fois depuis WSL :  ./creer_raccourci.sh
set -euo pipefail

if ! command -v powershell.exe >/dev/null 2>&1; then
    echo "powershell.exe est introuvable : ce script doit être lancé depuis WSL." >&2
    exit 1
fi
if [ -z "${WSL_DISTRO_NAME:-}" ]; then
    echo "Distribution WSL non détectée (variable WSL_DISTRO_NAME absente)." >&2
    exit 1
fi

project="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
name="${1:-Traitement SPED}"
arguments="-d ${WSL_DISTRO_NAME} --cd \"${project}\" -e bash -lc \"source venv/bin/activate && python run_all.py\""

# Doubler les apostrophes pour les chaînes PowerShell entre apostrophes.
quote="'"
ps_arguments="${arguments//$quote/$quote$quote}"
ps_name="${name//$quote/$quote$quote}"

powershell.exe -NoProfile -NonInteractive -Command "
\$desktop = [Environment]::GetFolderPath('Desktop')
\$link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path \$desktop '${ps_name}.lnk'))
\$link.TargetPath = Join-Path \$env:SystemRoot 'System32\\wsl.exe'
\$link.Arguments = '${ps_arguments}'
\$link.WorkingDirectory = \$env:USERPROFILE
\$link.Description = 'Importe les nouveaux lots puis lance codes-barres, OCR et classification finale'
\$link.Save()
Write-Output (Join-Path \$desktop '${ps_name}.lnk')
" | tr -d '\r'
echo "Raccourci créé. Double-cliquez dessus pour lancer le traitement."
