# =============================================================
#  SERO — Sistema de Regularização de Obras
#  Aplicação Web — Streamlit
#  Lógica de negócio baseada nos módulos originais do projeto,
#  com as correções de divergência aplicadas conforme relatório.
# =============================================================

import streamlit as st
import pandas as pd
import re
import unicodedata
import sqlite3
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime
from dateutil.relativedelta import relativedelta
from io import BytesIO
import logging

# ──────────────────────────────────────────────
# CONFIGURAÇÃO DA PÁGINA
# ──────────────────────────────────────────────
st.set_page_config(
    page_title="SERO — Regularização de Obras",
    page_icon="🏗️",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ──────────────────────────────────────────────
# UTILITÁRIOS (matematica.py + formatadores.py)
# ──────────────────────────────────────────────

def arredondar_financeiro(valor) -> Decimal:
    """ROUND_HALF_UP para 2 casas decimais."""
    try:
        if isinstance(valor, float):
            valor = str(valor)
        return Decimal(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        return Decimal("0.00")


def formatar_moeda(valor) -> str:
    """Formata para padrão brasileiro: R$ 1.234,56"""
    return f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ──────────────────────────────────────────────
# MÓDULO DE DECADÊNCIA  (decadencia.py)
# CORREÇÃO APLICADA: +1 no mês final para alinhar
# com a fórmula da planilha: MONTH(fim)+1
# ──────────────────────────────────────────────

class CalculadoraDecadencia:
    def __init__(self, data_inicial: datetime, data_final: datetime):
        self.data_inicial = data_inicial
        self.data_final   = data_final
        self.data_hoje    = datetime.now()

    def calcular_prazos(self):
        data_decadencia = self.data_final + relativedelta(years=5)
        meses_dos_anos  = (self.data_final.year - self.data_inicial.year) * 12
        # CORREÇÃO: +1 no mês final (planilha usa MONTH(fim)+1)
        meses_totais    = meses_dos_anos + (self.data_final.month + 1) - self.data_inicial.month
        is_decadente    = self.data_hoje > data_decadencia
        return meses_totais, is_decadente, data_decadencia


# ──────────────────────────────────────────────
# MÓDULO DE ORÇAMENTO  (orcamento.py)
# CORREÇÕES APLICADAS:
#   1. obter_area_equivalente() → round(..., 2)
#   2. obter_valor_cod()        → usa área arredondada
#   3. calcular_rmt_bruto()     → cascata da área arredondada
# ──────────────────────────────────────────────

class OrcamentoObra:
    _MULTIPLICADORES = {
        1: 0.89, 2: 0.85, 3: 0.90, 4: 0.86,  5: 0.86,
        6: 0.83, 7: 0.98, 8: 0.95, 9: 0.86, 10: 0.83,
    }
    _REDUTORES_COMPLEMENTAR = {"Coberta": 0.50, "Descoberta": 0.25}

    def __init__(self, area_total: float, area_complementar: float,
                 redutor_complementar: str, valor_vau: float,
                 tipo_categoria: int, destinacao: int,
                 tipo_obra: str, material: str):
        self.area_total           = area_total
        self.area_complementar    = area_complementar
        self.redutor_complementar = redutor_complementar
        self.valor_vau            = valor_vau
        self.tipo_categoria       = tipo_categoria
        self.destinacao           = destinacao
        self.tipo_obra            = tipo_obra.strip().title()
        self.material             = material.strip().title()

    def obter_fator_categoria(self) -> float:
        return {1: 1.0, 2: 0.10, 3: 0.35}.get(self.tipo_categoria, 1.0)

    def obter_area_equivalente(self) -> float:
        """CORREÇÃO: resultado arredondado a 2 decimais (planilha usa ROUND(...,2))."""
        fator   = self._MULTIPLICADORES.get(self.destinacao, 1.0)
        redutor = self._REDUTORES_COMPLEMENTAR.get(self.redutor_complementar, 0.25)
        resultado = self.area_total * fator + self.area_complementar * redutor
        return round(resultado, 2)

    def obter_fator_social(self) -> float:
        area = self.area_total
        if   area <= 100: return 0.20
        elif area <= 200: return 0.40
        elif area <= 300: return 0.55
        elif area <= 400: return 0.70
        else:             return 0.90

    def obter_fator_ajuste_material(self) -> float:
        populares = ["Casa Popular", "Conjunto Habitacional Popular"]
        cat = "Popular" if self.tipo_obra in populares else "Padrao"
        tabela = {
            "Padrao":  {"Alvenaria": 0.20, "Madeira": 0.15, "Mista": 0.15},
            "Popular": {"Alvenaria": 0.12, "Madeira": 0.07, "Mista": 0.07},
        }
        return tabela.get(cat, {}).get(self.material, 1.0)

    def obter_valor_cod(self) -> Decimal:
        """COD = VAU × Área Equivalente (já arredondada)."""
        return arredondar_financeiro(self.valor_vau * self.obter_area_equivalente())

    def calcular_rmt_bruto(self) -> Decimal:
        """RMT = COD × Fator Social × Fator Material × Fator Categoria."""
        cod = float(self.obter_valor_cod())
        resultado = (
            cod
            * self.obter_fator_social()
            * self.obter_fator_ajuste_material()
            * self.obter_fator_categoria()
        )
        return arredondar_financeiro(resultado)


# ──────────────────────────────────────────────
# MÓDULO DE INSS  (inss.py)
# CORREÇÕES APLICADAS:
#   1. Dedução concreto = COD × pct × 5%  (planilha: D29 × 5%)
#   2. INSS calculado sobre RMT_após concreto (não RMT_antes)
#   3. Decimal('0.03') e Decimal('0.058') (bug de string corrigido)
# ──────────────────────────────────────────────

class CalculadoraINSS:
    def __init__(self, rmt_antes: Decimal, cod: Decimal, fator_concreto: float):
        self.rmt_antes      = arredondar_financeiro(rmt_antes)
        self.cod            = arredondar_financeiro(cod)
        self.fator_concreto = Decimal(str(fator_concreto))

    def calcular_deducao_concreto(self) -> Decimal:
        """CORREÇÃO: COD × pct_concreto × 5% (fórmula D30 da planilha)."""
        return arredondar_financeiro(self.cod * self.fator_concreto * Decimal("0.05"))

    def calcular_rmt_apos(self) -> Decimal:
        return arredondar_financeiro(self.rmt_antes - self.calcular_deducao_concreto())

    def calcular_parcelas(self):
        base = self.calcular_rmt_apos()
        return {
            "patronal": arredondar_financeiro(base * Decimal("0.20")),
            "segurado": arredondar_financeiro(base * Decimal("0.08")),
            "rat":      arredondar_financeiro(base * Decimal("0.03")),
            "outras":   arredondar_financeiro(base * Decimal("0.058")),
        }

    def calcular_total(self) -> Decimal:
        p = self.calcular_parcelas()
        return p["patronal"] + p["segurado"] + p["rat"] + p["outras"]


# ──────────────────────────────────────────────
# MÓDULO DE CONCRETO  (concreto.py)
# Banco SQLite em memória — sem dependência de arquivo externo
# ──────────────────────────────────────────────

_CONCRETO_REGISTROS = [
    # (destinacao, uf, fator)
    # Residencial Unifamiliar ≤ 1000 m²
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "AC", 0.0743),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "AL", 0.0611),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "AM", 0.0743),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "AP", 0.0748),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "BA", 0.0553),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "CE", 0.0572),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "DF", 0.0524),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "ES", 0.0515),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "GO", 0.0579),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "MA", 0.0694),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "MG", 0.0468),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "MS", 0.0674),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "MT", 0.0622),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "PA", 0.0758),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "PB", 0.0632),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "PE", 0.0512),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "PI", 0.0533),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "PR", 0.0491),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "RJ", 0.0494),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "RN", 0.0596),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "RO", 0.0622),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "RR", 0.0743),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "RS", 0.0501),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "SC", 0.0479),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "SE", 0.0697),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "SP", 0.0490),
    ("Residencial Unifamiliar \u2264 1000 m\u00b2", "TO", 0.0533),
    # Residencial Unifamiliar ≥ 1001 m²
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "AC", 0.0743),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "AL", 0.0611),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "AM", 0.0743),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "AP", 0.0748),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "BA", 0.0553),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "CE", 0.0572),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "DF", 0.0524),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "ES", 0.0515),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "GO", 0.0579),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "MA", 0.0694),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "MG", 0.0468),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "MS", 0.0674),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "MT", 0.0622),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "PA", 0.0758),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "PB", 0.0632),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "PE", 0.0512),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "PI", 0.0533),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "PR", 0.0491),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "RJ", 0.0494),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "RN", 0.0596),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "RO", 0.0622),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "RR", 0.0743),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "RS", 0.0501),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "SC", 0.0479),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "SE", 0.0697),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "SP", 0.0490),
    ("Residencial Unifamiliar \u2265 1001 m\u00b2", "TO", 0.0533),
    # Residencial Multifamiliar ≤ 1000 m²
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "AC", 0.0961),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "AL", 0.0812),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "AM", 0.0961),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "AP", 0.0941),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "BA", 0.0746),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "CE", 0.0769),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "DF", 0.0706),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "ES", 0.0685),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "GO", 0.0762),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "MA", 0.0873),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "MG", 0.0622),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "MS", 0.0874),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "MT", 0.0801),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "PA", 0.0977),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "PB", 0.0858),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "PE", 0.0689),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "PI", 0.0716),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "PR", 0.0650),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "RJ", 0.0652),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "RN", 0.0762),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "RO", 0.0801),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "RR", 0.0961),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "RS", 0.0654),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "SC", 0.0619),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "SE", 0.0905),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "SP", 0.0635),
    ("Residencial Multifamiliar \u2264 1000 m\u00b2", "TO", 0.0716),
    # Residencial Multifamiliar ≥ 1001 m²
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "AC", 0.0961),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "AL", 0.0812),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "AM", 0.0961),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "AP", 0.0941),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "BA", 0.0746),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "CE", 0.0769),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "DF", 0.0706),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "ES", 0.0685),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "GO", 0.0762),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "MA", 0.0873),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "MG", 0.0622),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "MS", 0.0874),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "MT", 0.0801),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "PA", 0.0977),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "PB", 0.0858),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "PE", 0.0689),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "PI", 0.0716),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "PR", 0.0650),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "RJ", 0.0652),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "RN", 0.0762),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "RO", 0.0801),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "RR", 0.0961),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "RS", 0.0654),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "SC", 0.0619),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "SE", 0.0905),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "SP", 0.0635),
    ("Residencial Multifamiliar \u2265 1001 m\u00b2", "TO", 0.0716),
    # Casa Popular
    ("Casa Popular", "AC", 0.0469),
    ("Casa Popular", "AL", 0.0398),
    ("Casa Popular", "AM", 0.0469),
    ("Casa Popular", "AP", 0.0488),
    ("Casa Popular", "BA", 0.0373),
    ("Casa Popular", "CE", 0.0370),
    ("Casa Popular", "DF", 0.0353),
    ("Casa Popular", "ES", 0.0333),
    ("Casa Popular", "GO", 0.0388),
    ("Casa Popular", "MA", 0.0418),
    ("Casa Popular", "MG", 0.0315),
    ("Casa Popular", "MS", 0.0434),
    ("Casa Popular", "MT", 0.0402),
    ("Casa Popular", "PA", 0.0491),
    ("Casa Popular", "PB", 0.0412),
    ("Casa Popular", "PE", 0.0351),
    ("Casa Popular", "PI", 0.0353),
    ("Casa Popular", "PR", 0.0318),
    ("Casa Popular", "RJ", 0.0320),
    ("Casa Popular", "RN", 0.0401),
    ("Casa Popular", "RO", 0.0402),
    ("Casa Popular", "RR", 0.0469),
    ("Casa Popular", "RS", 0.0325),
    ("Casa Popular", "SC", 0.0293),
    ("Casa Popular", "SE", 0.0434),
    ("Casa Popular", "SP", 0.0315),
    ("Casa Popular", "TO", 0.0353),
    # Comercial Salas e Lojas ≤ 3000 m²
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "AC", 0.1333),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "AL", 0.1135),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "AM", 0.1333),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "AP", 0.1293),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "BA", 0.1031),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "CE", 0.1069),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "DF", 0.0962),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "ES", 0.0945),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "GO", 0.1027),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "MA", 0.1206),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "MG", 0.0866),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "MS", 0.1220),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "MT", 0.1096),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "PA", 0.1348),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "PB", 0.1181),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "PE", 0.0974),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "PI", 0.1000),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "PR", 0.0878),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "RJ", 0.0902),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "RN", 0.1041),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "RO", 0.1096),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "RR", 0.1333),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "RS", 0.0877),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "SC", 0.0836),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "SE", 0.1250),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "SP", 0.0869),
    ("Comercial Salas e Lojas \u2264 3000 m\u00b2", "TO", 0.1000),
    # Comercial Salas e Lojas ≥ 3001 m²
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "AC", 0.1333),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "AL", 0.1135),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "AM", 0.1333),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "AP", 0.1293),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "BA", 0.1031),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "CE", 0.1069),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "DF", 0.0962),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "ES", 0.0945),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "GO", 0.1027),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "MA", 0.1206),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "MG", 0.0866),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "MS", 0.1220),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "MT", 0.1096),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "PA", 0.1348),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "PB", 0.1181),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "PE", 0.0974),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "PI", 0.1000),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "PR", 0.0878),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "RJ", 0.0902),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "RN", 0.1041),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "RO", 0.1096),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "RR", 0.1333),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "RS", 0.0877),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "SC", 0.0836),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "SE", 0.1250),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "SP", 0.0869),
    ("Comercial Salas e Lojas \u2265 3001 m\u00b2", "TO", 0.1000),
    # Edifício de Garagens ≤ 3000 m²  (coluna "Edifício de Garagens" da planilha)
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "AC", 0.1333),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "AL", 0.1135),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "AM", 0.1333),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "AP", 0.1293),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "BA", 0.1031),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "CE", 0.1069),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "DF", 0.0962),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "ES", 0.0945),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "GO", 0.1027),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "MA", 0.1206),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "MG", 0.0866),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "MS", 0.1220),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "MT", 0.1096),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "PA", 0.1348),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "PB", 0.1181),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "PE", 0.0974),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "PI", 0.1000),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "PR", 0.0878),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "RJ", 0.0902),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "RN", 0.1041),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "RO", 0.1096),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "RR", 0.1333),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "RS", 0.0877),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "SC", 0.0836),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "SE", 0.1250),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "SP", 0.0869),
    ("Edif\u00edcio de Garagens \u2264 3000 m\u00b2", "TO", 0.1000),
    # Edifício de Garagens ≥ 3001 m²  (mesma coluna da planilha)
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "AC", 0.1333),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "AL", 0.1135),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "AM", 0.1333),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "AP", 0.1293),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "BA", 0.1031),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "CE", 0.1069),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "DF", 0.0962),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "ES", 0.0945),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "GO", 0.1027),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "MA", 0.1206),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "MG", 0.0866),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "MS", 0.1220),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "MT", 0.1096),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "PA", 0.1348),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "PB", 0.1181),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "PE", 0.0974),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "PI", 0.1000),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "PR", 0.0878),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "RJ", 0.0902),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "RN", 0.1041),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "RO", 0.1096),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "RR", 0.1333),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "RS", 0.0877),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "SC", 0.0836),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "SE", 0.1250),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "SP", 0.0869),
    ("Edif\u00edcio de Garagens \u2265 3001 m\u00b2", "TO", 0.1000),
    # Galpão Industrial
    ("Galp\u00e3o Industrial", "AC", 0.0452),
    ("Galp\u00e3o Industrial", "AL", 0.0382),
    ("Galp\u00e3o Industrial", "AM", 0.0452),
    ("Galp\u00e3o Industrial", "AP", 0.0438),
    ("Galp\u00e3o Industrial", "BA", 0.0362),
    ("Galp\u00e3o Industrial", "CE", 0.0344),
    ("Galp\u00e3o Industrial", "DF", 0.0343),
    ("Galp\u00e3o Industrial", "ES", 0.0326),
    ("Galp\u00e3o Industrial", "GO", 0.0360),
    ("Galp\u00e3o Industrial", "MA", 0.0407),
    ("Galp\u00e3o Industrial", "MG", 0.0305),
    ("Galp\u00e3o Industrial", "MS", 0.0428),
    ("Galp\u00e3o Industrial", "MT", 0.0389),
    ("Galp\u00e3o Industrial", "PA", 0.0445),
    ("Galp\u00e3o Industrial", "PB", 0.0381),
    ("Galp\u00e3o Industrial", "PE", 0.0342),
    ("Galp\u00e3o Industrial", "PI", 0.0330),
    ("Galp\u00e3o Industrial", "PR", 0.0308),
    ("Galp\u00e3o Industrial", "RJ", 0.0308),
    ("Galp\u00e3o Industrial", "RN", 0.0363),
    ("Galp\u00e3o Industrial", "RO", 0.0389),
    ("Galp\u00e3o Industrial", "RR", 0.0452),
    ("Galp\u00e3o Industrial", "RS", 0.0323),
    ("Galp\u00e3o Industrial", "SC", 0.0287),
    ("Galp\u00e3o Industrial", "SE", 0.0418),
    ("Galp\u00e3o Industrial", "SP", 0.0296),
    ("Galp\u00e3o Industrial", "TO", 0.0330),
]

_conn_concreto = sqlite3.connect(":memory:")
_conn_concreto.execute(
    "CREATE TABLE concreto (destinacao TEXT, uf TEXT, fator REAL, "
    "PRIMARY KEY (destinacao, uf))"
)
_conn_concreto.executemany(
    "INSERT INTO concreto VALUES (?, ?, ?)", _CONCRETO_REGISTROS
)
_conn_concreto.commit()


def get_fator(destinacao: str, uf: str) -> float:
    cur = _conn_concreto.execute(
        "SELECT fator FROM concreto WHERE destinacao = ? AND uf = ?",
        (destinacao, uf.strip().upper()),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(
            f"Combinação não encontrada na tabela de concreto: "
            f"destinacao='{destinacao}', uf='{uf}'"
        )
    return float(row[0])


# ──────────────────────────────────────────────
# MAPEAMENTOS DE INTERFACE
# ──────────────────────────────────────────────

CATEGORIAS = {
    "Obra nova / Acréscimo": 1,
    "Demolição":             2,
    "Reforma":               3,
}

DESTINACOES = {
    "Residencial Unifamiliar até 1.000 m²":          (1, "Residencial Unifamiliar \u2264 1000 m\u00b2"),
    "Residencial Unifamiliar acima de 1.000 m²":     (2, "Residencial Unifamiliar \u2265 1001 m\u00b2"),
    "Residencial Multifamiliar até 1.000 m²":        (3, "Residencial Multifamiliar \u2264 1000 m\u00b2"),
    "Residencial Multifamiliar acima de 1.000 m²":   (4, "Residencial Multifamiliar \u2265 1001 m\u00b2"),
    "Comercial / Salas e Lojas até 3.000 m²":        (5, "Comercial Salas e Lojas \u2264 3000 m\u00b2"),
    "Comercial / Salas e Lojas acima de 3.000 m²":   (6, "Comercial Salas e Lojas \u2265 3001 m\u00b2"),
    "Casa Popular":                                  (7, "Casa Popular"),
    "Galpão Industrial":                             (8, "Galp\u00e3o Industrial"),
    "Edifício de Garagens até 3.000 m²":             (9, "Edif\u00edcio de Garagens \u2264 3000 m\u00b2"),
    "Edifício de Garagens acima de 3.000 m²":       (10, "Edif\u00edcio de Garagens \u2265 3001 m\u00b2"),
}

TIPOS_OBRA = [
    "Alvenaria",
    "Madeira",
    "Mista",
]

TIPOS_USO = [
    "Residencial",
    "Comercial",
    "Casa Popular",
    "Conjunto Habitacional Popular",
]

REDUTORES = ["Descoberta", "Coberta"]

UFS_BRASIL = [
    "AC","AL","AP","AM","BA","CE","DF","ES","GO","MA",
    "MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN",
    "RS","RO","RR","SC","SP","SE","TO",
]


# ──────────────────────────────────────────────
# INTERFACE STREAMLIT
# ──────────────────────────────────────────────

def main():
    # ── Header ──────────────────────────────
    st.markdown(
        """
        <div style='text-align:center; padding: 1rem 0 0.5rem 0;'>
            <h1 style='color:#1f4e79;'>🏗️ SERO</h1>
            <h4 style='color:#2e75b6; font-weight:400;'>Sistema de Regularização de Obras</h4>
            <p style='color:#555;'>Aferição Indireta de INSS — Pessoa Física</p>
        </div>
        <hr style='border:1px solid #d0d0d0;'>
        """,
        unsafe_allow_html=True,
    )

    df_concreto = True  # dados já incorporados no banco SQLite em memória

    st.markdown("---")

    # ── PASSO 1: Prazos ──────────────────────
    st.subheader("📅 Passo 1 — Período da Obra")
    col1, col2 = st.columns(2)
    with col1:
        data_ini = st.date_input("Data de **início** da obra", format="DD/MM/YYYY")
    with col2:
        data_fim = st.date_input("Data de **conclusão** da obra", format="DD/MM/YYYY")

    st.markdown("---")

    # ── PASSO 2: Dados da Obra ───────────────
    st.subheader("📐 Passo 2 — Dados da Obra")

    col_a, col_b = st.columns(2)
    with col_a:
        area_total = st.number_input(
            "Área principal a ser aferida (m²)", min_value=0.01,
            value=214.85, step=0.01, format="%.2f"
        )
        area_comp = st.number_input(
            "Área complementar (m²) — deixe 0 se não houver",
            min_value=0.0, value=0.0, step=0.01, format="%.2f"
        )
        redutor_comp = st.selectbox("Tipo de área complementar", REDUTORES,
                                    help="Coberta = 50% | Descoberta = 25%")
    with col_b:
        vau_input = st.number_input(
            "Valor do VAU — R$/m² (tabela da prefeitura)",
            min_value=0.01, value=2962.67, step=0.01, format="%.2f"
        )
        categoria_label = st.selectbox("Categoria da obra", list(CATEGORIAS.keys()))
        destinacao_label = st.selectbox("Destinação", list(DESTINACOES.keys()))

    col_c, col_d = st.columns(2)
    with col_c:
        material = st.selectbox("Tipo de material construtivo", TIPOS_OBRA)
    with col_d:
        tipo_uso = st.selectbox("Tipo de uso / destinação social", TIPOS_USO)

    st.markdown("---")

    # ── PASSO 3: Concreto Usinado ────────────
    st.subheader("🪨 Passo 3 — Concreto Usinado")
    tem_concreto = st.radio(
        "A obra utilizou concreto usinado?", ["Não", "Sim"], horizontal=True
    )
    fator_concreto = 0.0
    uf_selecionada = None

    if tem_concreto == "Sim":
        if df_concreto is None:
            st.warning("⚠️ Dados de concreto não disponíveis.")
        else:
            uf_selecionada = st.selectbox("UF onde a obra foi realizada", UFS_BRASIL, index=UFS_BRASIL.index("MG"))
            dest_id, dest_nome_planilha = DESTINACOES[destinacao_label]
            try:
                pct = get_fator(dest_nome_planilha, uf_selecionada)
            except ValueError:
                pct = None
            if pct is None:
                st.error(f"UF '{uf_selecionada}' não encontrada na tabela de concreto.")
            else:
                fator_concreto = pct
                st.info(f"📊 Percentual de concreto usinado para **{uf_selecionada}** "
                        f"({destinacao_label}): **{pct * 100:.2f}%**")

    st.markdown("---")

    # ── CALCULAR ────────────────────────────
    calcular = st.button("⚙️ Calcular INSS", type="primary", use_container_width=True)

    if calcular:
        # --- Validações básicas ---
        erros = []
        if data_fim <= data_ini:
            erros.append("A data de conclusão deve ser posterior à data de início.")
        if area_total <= 0:
            erros.append("A área total deve ser maior que zero.")
        if vau_input <= 0:
            erros.append("O VAU deve ser maior que zero.")

        if erros:
            for e in erros:
                st.error(f"❌ {e}")
            st.stop()

        # --- Decadência ---
        dt_ini = datetime(data_ini.year, data_ini.month, data_ini.day)
        dt_fim = datetime(data_fim.year, data_fim.month, data_fim.day)
        calc_dec = CalculadoraDecadencia(dt_ini, dt_fim)
        meses, is_decadente, data_dec = calc_dec.calcular_prazos()

        # --- Orçamento ---
        dest_id, dest_nome_planilha = DESTINACOES[destinacao_label]
        orcamento = OrcamentoObra(
            area_total        = area_total,
            area_complementar = area_comp,
            redutor_complementar = redutor_comp,
            valor_vau         = vau_input,
            tipo_categoria    = CATEGORIAS[categoria_label],
            destinacao        = dest_id,
            tipo_obra         = tipo_uso,
            material          = material,
        )
        area_equiv    = orcamento.obter_area_equivalente()
        cod           = orcamento.obter_valor_cod()
        fator_cat     = orcamento.obter_fator_categoria()
        fator_soc     = orcamento.obter_fator_social()
        fator_mat     = orcamento.obter_fator_ajuste_material()
        rmt_bruto     = orcamento.calcular_rmt_bruto()

        # --- INSS ---
        calc_inss = CalculadoraINSS(rmt_bruto, cod, fator_concreto)
        deducao       = calc_inss.calcular_deducao_concreto()
        rmt_apos      = calc_inss.calcular_rmt_apos()
        parcelas      = calc_inss.calcular_parcelas()
        inss_total    = calc_inss.calcular_total()

        # ──────────────────────────────────────
        # EXIBIÇÃO DOS RESULTADOS
        # ──────────────────────────────────────
        st.markdown("---")
        st.markdown("## 📋 Resultado do Cálculo")

        # ── Bloco 1: Decadência ──
        st.markdown("### 📅 Situação Temporal da Obra")
        status_color = "🔴" if is_decadente else "🟢"
        status_texto = "**DECADENTE** — prazo de 5 anos encerrado" if is_decadente else "**Não decadente** — dentro do prazo legal"
        col1, col2, col3 = st.columns(3)
        col1.metric("Duração da obra", f"{meses} meses")
        col2.metric("Prazo de decadência", data_dec.strftime("%d/%m/%Y"))
        col3.metric("Status", f"{status_color} {('Decadente' if is_decadente else 'Válida')}")
        st.info(f"{status_color} {status_texto}")

        st.markdown("---")

        # ── Bloco 2: Fatores de Cálculo ──
        st.markdown("### 🔢 Fatores e Base de Cálculo")
        df_fatores = pd.DataFrame({
            "Componente":    ["Área Total", "Área Complementar", "Área Equivalente",
                              "Fator Categoria", "Fator Social", "Fator Material"],
            "Valor":         [
                f"{area_total:.2f} m²",
                f"{area_comp:.2f} m²",
                f"{area_equiv:.2f} m²",
                f"{fator_cat * 100:.0f}%",
                f"{fator_soc * 100:.0f}%",
                f"{fator_mat * 100:.0f}%",
            ],
        })
        st.dataframe(df_fatores, use_container_width=True, hide_index=True)

        st.markdown("---")

        # ── Bloco 3: RMT ──
        st.markdown("### 🏗️ Custo de Referência da Obra (RMT)")
        colA, colB, colC = st.columns(3)
        colA.metric("COD (Base bruta)", formatar_moeda(cod))
        colB.metric("RMT antes do concreto", formatar_moeda(rmt_bruto))

        if fator_concreto > 0:
            colC.metric(
                "Dedução concreto usinado",
                f"− {formatar_moeda(deducao)}",
                delta=f"−{fator_concreto*100:.2f}% × 5%",
                delta_color="inverse",
            )
            st.metric(
                "✅ RMT após abatimento (base de cálculo do INSS)",
                formatar_moeda(rmt_apos),
            )
        else:
            colC.metric("Dedução concreto", "R$ 0,00")
            st.metric("✅ RMT (base de cálculo do INSS)", formatar_moeda(rmt_apos))

        st.markdown("---")

        # ── Bloco 4: INSS ──
        st.markdown("### 💰 Encargos de INSS")
        df_inss = pd.DataFrame({
            "Rubrica": [
                "20% — INSS Patronal",
                "8%  — INSS Segurado",
                "3%  — RAT (Risco Ambiental do Trabalho)",
                "5,8% — Outras Entidades (Sistema S)",
            ],
            "Alíquota": ["20,00%", "8,00%", "3,00%", "5,80%"],
            "Valor":    [
                formatar_moeda(parcelas["patronal"]),
                formatar_moeda(parcelas["segurado"]),
                formatar_moeda(parcelas["rat"]),
                formatar_moeda(parcelas["outras"]),
            ],
        })
        st.dataframe(df_inss, use_container_width=True, hide_index=True)

        st.markdown(
            f"""
            <div style='background:#1f4e79; border-radius:8px; padding:1rem 1.5rem; margin-top:1rem;'>
                <p style='color:#cce4ff; font-size:0.9rem; margin:0;'>INSS TOTAL A RECOLHER (36,8%)</p>
                <p style='color:#ffffff; font-size:2rem; font-weight:bold; margin:0;'>
                    {formatar_moeda(inss_total)}
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # ── Bloco 5: Resumo exportável ──
        st.markdown("---")
        st.markdown("### 📄 Resumo Completo")
        resumo = f"""
SERO — Aferição Indireta de INSS
{'='*45}
Data de cálculo   : {datetime.now().strftime('%d/%m/%Y %H:%M')}

PERÍODO DA OBRA
  Início          : {data_ini.strftime('%d/%m/%Y')}
  Fim             : {data_fim.strftime('%d/%m/%Y')}
  Duração         : {meses} meses
  Decadência em   : {data_dec.strftime('%d/%m/%Y')}
  Status          : {'DECADENTE' if is_decadente else 'Não decadente'}

DADOS DA OBRA
  VAU             : {formatar_moeda(vau_input)}
  Área total      : {area_total:.2f} m²
  Área complement.: {area_comp:.2f} m²
  Área equivalente: {area_equiv:.2f} m²
  Categoria       : {categoria_label}
  Destinação      : {destinacao_label}
  Material        : {material}
  Tipo de uso     : {tipo_uso}

FATORES
  Categoria       : {fator_cat*100:.0f}%
  Social          : {fator_soc*100:.0f}%
  Material        : {fator_mat*100:.0f}%

BASE DE CÁLCULO
  COD             : {formatar_moeda(cod)}
  RMT (antes)     : {formatar_moeda(rmt_bruto)}
  Concreto ({uf_selecionada or 'N/A'}) : {fator_concreto*100:.2f}%
  Dedução concr.  : {formatar_moeda(deducao)}
  RMT (após)      : {formatar_moeda(rmt_apos)}

INSS
  Patronal (20%)  : {formatar_moeda(parcelas['patronal'])}
  Segurado (8%)   : {formatar_moeda(parcelas['segurado'])}
  RAT (3%)        : {formatar_moeda(parcelas['rat'])}
  Outras (5,8%)   : {formatar_moeda(parcelas['outras'])}
{'='*45}
  TOTAL (36,8%)   : {formatar_moeda(inss_total)}
{'='*45}
        """.strip()

        st.text_area("Copie ou salve o resumo abaixo:", value=resumo, height=400)
        st.download_button(
            "⬇️ Baixar resumo (.txt)",
            data=resumo.encode("utf-8"),
            file_name=f"SERO_INSS_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
            mime="text/plain",
            use_container_width=True,
        )

    # ── Rodapé ──────────────────────────────
    st.markdown("---")
    st.markdown(
        "<p style='text-align:center; color:#aaa; font-size:0.8rem;'>"
        "SERO v1.0 — Uso exclusivo do aluno"
        "</p>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
