from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .agent_client import AgentUnavailable, read_status
from .views import administrator

CHECK_NAMES = {
    "platform.linux": "Sistema operacional",
    "platform.arch": "Arquitetura do processador",
    "platform.distribution": "Distribuição Linux",
    "systemd.pid1": "Gerenciador de serviços",
    "cgroup.v2": "Controle de recursos",
    "cgroup.cpu": "Compartilhamento de CPU",
    "cgroup.memory": "Controle de memória",
    "cgroup.pids": "Controle de processos",
    "cgroup.io": "Controle de leitura e escrita em disco",
    "psi.cpu": "Medição de pressão de CPU",
    "psi.memory": "Medição de pressão de memória",
    "psi.io": "Medição de pressão de disco",
    "namespaces.user": "Separação de identidades",
    "security.apparmor": "Proteção AppArmor",
    "capacity.memory": "Memória do servidor",
    "capacity.disk": "Espaço em disco",
    "tool.podman": "Podman",
    "tool.systemctl": "Ferramenta de serviços",
}
STATUS_NAMES = {
    "pass": "Verificado",
    "fail": "Requer atenção",
    "unknown": "Não verificado",
    "warning": "Aviso",
}


@login_required
@require_GET
def diagnostics(request):
    administrator(request)
    context = {}
    try:
        status = read_status()
        context["report"] = status["report"]
        for check in context["report"]["checks"]:
            check["name"] = CHECK_NAMES.get(check["code"], check["code"])
            check["status_name"] = STATUS_NAMES[check["status"]]
    except AgentUnavailable as exc:
        context["agent_error"] = str(exc)
    return render(request, "control/diagnostics.html", context)
