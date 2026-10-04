import json
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET
from .services import build_shared_live_snapshot


def index(request):
    return render(request, "dashboard/home.html")


@require_GET
def snapshot(request):
    office = request.GET.get("office", "president")
    mode = request.GET.get("mode", "live")
    if mode != "live":
        return JsonResponse({"error": "Modo inválido."}, status=400)
    try:
        turn = int(request.GET.get("turn", "1"))
        if turn not in {1, 2}:
            raise ValueError("O turno deve ser 1 ou 2.")
        data = build_shared_live_snapshot(office, turn)
        return JsonResponse(data, json_dumps_params={"ensure_ascii": False})
    except (ValueError, LookupError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": str(exc), "mode": mode, "office": office}, status=503)
