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
# ──────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def carregar_tabela_concreto(arquivo_bytes: bytes) -> pd.DataFrame | None:
    """Carrega a aba 'Tabela Concreto Usinado' do Excel."""
    try:
        xl = pd.ExcelFile(BytesIO(arquivo_bytes))
        aba = "Tabela Concreto Usinado"
        if aba not in xl.sheet_names:
            st.error(f"Aba '{aba}' não encontrada no arquivo. Abas disponíveis: {xl.sheet_names}")
            return None

        df_raw = pd.read_excel(BytesIO(arquivo_bytes), sheet_name=aba, header=None)
        linha_cabecalho = None
        for i, row in df_raw.iterrows():
            if any(isinstance(c, str) and c.strip().upper() == "UF" for c in row):
                linha_cabecalho = i
                break

        if linha_cabecalho is None:
            st.error("Não foi possível detectar o cabeçalho 'UF' na tabela de concreto.")
            return None

        df = pd.read_excel(BytesIO(arquivo_bytes), sheet_name=aba, header=linha_cabecalho)
        df.dropna(how="all", inplace=True)
        df.reset_index(drop=True, inplace=True)

        col_uf = df.columns[0]
        for col in df.columns[1:]:
            df[col] = df[col].apply(_converter_percentual)

        df[col_uf] = df[col_uf].astype(str).str.strip().str.upper().apply(_normalizar_texto)
        df = df[df[col_uf] != "NAN"].reset_index(drop=True)
        return df
    except Exception as e:
        st.error(f"Erro ao carregar planilha: {e}")
        return None


def _converter_percentual(valor) -> float:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor) if abs(valor) < 1 else float(valor) / 100
    if isinstance(valor, str):
        texto = re.sub(r"%+", "", valor.strip()).replace(",", ".").strip()
        if not texto:
            return 0.0
        try:
            num = float(texto)
            return num if abs(num) < 1 else num / 100
        except ValueError:
            return 0.0
    return 0.0


def _normalizar_texto(texto: str) -> str:
    return (
        unicodedata.normalize("NFKD", texto)
        .encode("ASCII", "ignore")
        .decode("utf-8")
        .upper()
        .strip()
    )


def _extrair_palavras(texto) -> set:
    if not isinstance(texto, str):
        return set()
    norm = _normalizar_texto(texto).lower().replace("m²", "m2")
    palavras = set(re.findall(r"[a-z0-9]+", norm))
    return palavras - {"com", "de", "m2", "edificios", "mais", "ate", "e"}


def buscar_percentual_concreto(df: pd.DataFrame, uf: str, nome_destinacao: str) -> float:
    col_uf = df.columns[0]
    uf_norm = _normalizar_texto(uf)
    mascara = df[col_uf] == uf_norm
    if not mascara.any():
        return None  # UF não encontrada

    keys_menu = _extrair_palavras(nome_destinacao)
    melhor_col, maior_score = None, -1
    for col in df.columns[1:]:
        score = len(keys_menu & _extrair_palavras(str(col)))
        if score > maior_score:
            maior_score, melhor_col = score, col

    if melhor_col is None or maior_score == 0:
        return 0.0

    valor = df.loc[mascara, melhor_col].values[0]
    return 0.0 if pd.isna(valor) else float(valor)


# ──────────────────────────────────────────────
# MAPEAMENTOS DE INTERFACE
# ──────────────────────────────────────────────

CATEGORIAS = {
    "Obra nova / Acréscimo": 1,
    "Demolição":             2,
    "Reforma":               3,
}

DESTINACOES = {
    "Residencial Unifamiliar até 1.000 m²":          (1, "Residencial Unifamiliar ≤ 1000 m²"),
    "Residencial Unifamiliar acima de 1.000 m²":     (2, "Residencial Unifamiliar ≥ 1001 m²"),
    "Residencial Multifamiliar até 1.000 m²":        (3, "Residencial Multifamiliar ≤ 1000 m²"),
    "Residencial Multifamiliar acima de 1.000 m²":   (4, "Residencial Multifamiliar ≥ 1001 m²"),
    "Comercial / Salas e Lojas até 3.000 m²":        (5, "Comercial Salas e Lojas ≤ 3000 m²"),
    "Comercial / Salas e Lojas acima de 3.000 m²":   (6, "Comercial Salas e Lojas ≥ 3001 m²"),
    "Casa Popular":                                  (7, "Casa Popular"),
    "Galpão Industrial":                             (8, "Galpão Industrial"),
    "Edifício de Garagens até 3.000 m²":             (9, "Edifício de Garagens ≤ 3000 m²"),
    "Edifício de Garagens acima de 3.000 m²":       (10, "Edifício de Garagens ≥ 3001 m²"),
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

    # ── Upload da Planilha ───────────────────
    with st.expander("📂 Passo 0 — Carregar Planilha de Concreto Usinado", expanded=True):
        st.markdown(
            "Faça o upload da planilha Excel **SERO** (arquivo `.xlsx`). "
            "Ela contém a tabela de percentuais de concreto usinado por UF."
        )
        arquivo = st.file_uploader(
            "Selecione o arquivo .xlsx", type=["xlsx"], label_visibility="collapsed"
        )
        df_concreto = None
        if arquivo:
            df_concreto = carregar_tabela_concreto(arquivo.read())
            if df_concreto is not None:
                st.success(f"✅ Planilha carregada! {len(df_concreto)} estados encontrados.")

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
            st.warning("⚠️ Faça o upload da planilha no Passo 0 para buscar o percentual de concreto.")
        else:
            uf_selecionada = st.selectbox("UF onde a obra foi realizada", UFS_BRASIL, index=UFS_BRASIL.index("MG"))
            dest_id, dest_nome_planilha = DESTINACOES[destinacao_label]
            pct = buscar_percentual_concreto(df_concreto, uf_selecionada, dest_nome_planilha)
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
        "SERO v1.0 — Uso exclusivo do aluno | Material LUMEN TREINAMENTOS LTDA."
        "</p>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
