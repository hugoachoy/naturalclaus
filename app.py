import streamlit as st
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from pathlib import Path
from fpdf import FPDF
from datetime import datetime
import pandas as pd

# ─────────────────────────────────────────────
#  CONFIGURACIÓN GLOBAL
# ─────────────────────────────────────────────
LOGO_COLOR = "#468c45"
SPREADSHEET_NAME = "datos"   # ← Cambiá esto si tu archivo en Drive tiene otro nombre


# ─────────────────────────────────────────────
#  CAPA DE DATOS  (Google Sheets)
# ─────────────────────────────────────────────
def _get_sheet(sheet_name: str):
    """Devuelve la hoja de Google Sheets solicitada."""
    import json
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    if Path("credenciales.json").exists():
        # Entorno local: usa el archivo JSON
        creds = ServiceAccountCredentials.from_json_keyfile_name("credenciales.json", scope)
    else:
        # Streamlit Cloud: lee el JSON desde st.secrets
        creds_dict = json.loads(st.secrets["gcp_service_account"]["credentials_json"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)

    return gspread.authorize(creds).open(SPREADSHEET_NAME).worksheet(sheet_name)


@st.cache_data(ttl=60)   # ← Evita llamadas repetidas a la API en cada recarga
def _read(sheet_name: str) -> pd.DataFrame:
    """Lee una hoja y devuelve un DataFrame limpio."""
    try:
        sheet = _get_sheet(sheet_name)
        data = sheet.get_all_records()
        df = pd.DataFrame(data)
        return df.fillna("")
    except Exception as e:
        st.warning(f"No se pudo leer la hoja '{sheet_name}': {e}")
        return pd.DataFrame()


def _save(sheet_name: str, df: pd.DataFrame) -> bool:
    """
    Sobreescribe la hoja con el DataFrame recibido.
    Devuelve True si tuvo éxito, False si falló.
    """
    try:
        sheet = _get_sheet(sheet_name)
        # Reemplazamos NaN por cadena vacía para que Sheets no se queje
        df_clean = df.fillna("").astype(str)
        sheet.clear()
        sheet.update([df_clean.columns.tolist()] + df_clean.values.tolist())
        # Invalidamos la caché para que la próxima lectura traiga datos frescos
        _read.clear()
        return True
    except Exception as e:
        st.error(f"Error al guardar en '{sheet_name}': {e}")
        return False


# ─────────────────────────────────────────────
#  GENERACIÓN DE PDF
# ─────────────────────────────────────────────
def generar_pdf_precios(df_prod: pd.DataFrame) -> bytes:
    """Genera un PDF con la lista de precios y stock actual."""
    # Nos aseguramos de que las columnas numéricas sean numéricas
    for col in ["precio_compra", "precio_venta", "porcentaje_ganancia", "stock_actual"]:
        if col in df_prod.columns:
            df_prod[col] = pd.to_numeric(df_prod[col], errors="coerce").fillna(0)

    pdf = FPDF()
    pdf.add_page()

    # Encabezado
    pdf.set_font("helvetica", "B", 20)
    pdf.set_text_color(70, 140, 69)
    pdf.cell(0, 10, "Natural Clau's", ln=True, align="C")
    pdf.set_font("helvetica", "I", 12)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(0, 10, f"Lista de Precios - {datetime.now().strftime('%d/%m/%Y')}", ln=True, align="C")
    pdf.ln(8)

    # Cabecera de tabla
    pdf.set_fill_color(70, 140, 69)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 10)
    pdf.cell(65, 10, "Producto",  border=1, fill=True)
    pdf.cell(35, 10, "Costo",     border=1, fill=True, align="C")
    pdf.cell(35, 10, "Venta",     border=1, fill=True, align="C")
    pdf.cell(25, 10, "Stock",     border=1, fill=True, align="C")
    pdf.cell(30, 10, "% Gan.",    border=1, fill=True, align="C")
    pdf.ln()

    # Filas
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("helvetica", "", 10)

    for _, row in df_prod.iterrows():
        producto  = str(row.get("producto", ""))
        precio_c  = f"$ {float(row.get('precio_compra', 0)):,.2f}"
        precio_v  = f"$ {float(row.get('precio_venta', 0)):,.2f}"
        stock     = str(int(row.get("stock_actual", 0)))
        ganancia  = f"{float(row.get('porcentaje_ganancia', 0)):.1f}%"

        pdf.cell(65, 8, producto,  border=1)
        pdf.cell(35, 8, precio_c,  border=1, align="R")
        pdf.cell(35, 8, precio_v,  border=1, align="R")
        pdf.cell(25, 8, stock,     border=1, align="C")
        pdf.cell(30, 8, ganancia,  border=1, align="R")
        pdf.ln()

    return bytes(pdf.output())


# ─────────────────────────────────────────────
#  CÁLCULO DE STOCK
# ─────────────────────────────────────────────
def obtener_df_con_stock() -> pd.DataFrame:
    """
    Devuelve el DataFrame de productos enriquecido con la columna 'stock_actual'.
    stock_actual = stock_inicial + compras - ventas
    """
    df_prod = _read("productos")
    df_comp = _read("compras")
    df_vend = _read("pedido_detalle")

    if df_prod.empty:
        return pd.DataFrame()

    # Normalizamos id_producto a entero en los tres DataFrames
    for df in [df_prod, df_comp, df_vend]:
        if "id_producto" in df.columns:
            df["id_producto"] = (
                pd.to_numeric(df["id_producto"], errors="coerce")
                .fillna(0)
                .astype(int)
            )

    # Ingresos (compras) y egresos (ventas) por producto
    ingresos = (
        df_comp.groupby("id_producto")["cantidad"].sum()
        if not df_comp.empty
        else pd.Series(dtype=float)
    )
    egresos = (
        df_vend.groupby("id_producto")["cantidad"].sum()
        if not df_vend.empty
        else pd.Series(dtype=float)
    )

    def _calcular_stock(row):
        inicial  = pd.to_numeric(row.get("stock_inicial", 0), errors="coerce") or 0
        compras  = ingresos.get(row["id_producto"], 0)
        ventas   = egresos.get(row["id_producto"], 0)
        return inicial + compras - ventas

    df_prod["stock_actual"] = df_prod.apply(_calcular_stock, axis=1)
    return df_prod


# ─────────────────────────────────────────────
#  HELPER: ID seguro
# ─────────────────────────────────────────────
def _next_id(df: pd.DataFrame, col: str) -> int:
    """Calcula el siguiente ID de forma segura, ignorando valores no numéricos."""
    if df.empty or col not in df.columns:
        return 1
    max_val = pd.to_numeric(df[col], errors="coerce").max()
    return 1 if pd.isna(max_val) else int(max_val) + 1


# ─────────────────────────────────────────────
#  LAYOUT PRINCIPAL
# ─────────────────────────────────────────────
st.set_page_config(page_title="Natural Clau's", layout="wide")
st.markdown(
    f"<style>.main-title {{ color: {LOGO_COLOR}; font-size: 3rem; font-weight: bold; }}</style>",
    unsafe_allow_html=True,
)

col_l, col_t = st.columns([1, 5])
if Path("Logo.jpeg").exists():
    col_l.image("Logo.jpeg", width=100)
col_t.markdown("<p class='main-title'>NATURAL Clau's</p>", unsafe_allow_html=True)

tabs = st.tabs(["💰 Ventas", "🛒 Compras", "🍎 Productos", "📦 Stock", "👥 Clientes", "🚛 Proveedores"])


# ─────────────────────────────────────────────
#  💰 VENTAS
# ─────────────────────────────────────────────
with tabs[0]:
    st.subheader("Gestión de Ventas")
    df_v   = _read("pedidos")
    df_vd  = _read("pedido_detalle")
    df_p_v = _read("productos")
    df_cl  = _read("clientes")

    col1, col2 = st.columns([1, 2])

    with col1:
        st.write("### Nueva Venta")
        with st.form("form_venta", clear_on_submit=True):

            cliente_lista = (
                df_cl["nombre_apellido"].dropna().unique().tolist()
                if not df_cl.empty
                else ["Consumidor Final"]
            )
            cliente_sel = st.selectbox(
                "Cliente", cliente_lista,
                index=None, placeholder="Escribe para buscar el cliente..."
            )

            prod_lista = (
                df_p_v["producto"].dropna().unique().tolist()
                if not df_p_v.empty else []
            )
            prod_sel = st.selectbox(
                "Producto", prod_lista,
                index=None, placeholder="Escribe para buscar el producto..."
            )

            cant      = st.number_input("Cantidad (grs/u)", min_value=0, step=100)
            forma_pago = st.selectbox("Medio de Pago", ["Efectivo", "Transferencia"])

            if st.form_submit_button("Registrar Venta"):
                if cliente_sel and prod_sel and cant > 0:
                    try:
                        prod_row  = df_p_v[df_p_v["producto"] == prod_sel].iloc[0]
                        id_p      = prod_row["id_producto"]
                        precio_v  = pd.to_numeric(prod_row["precio_venta"], errors="coerce") or 0
                        subtotal  = (cant / 1000) * precio_v
                        nuevo_id  = _next_id(df_v, "id_pedido")

                        nueva_v = pd.DataFrame([{
                            "id_pedido":  nuevo_id,
                            "fecha":      datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "cliente":    cliente_sel,
                            "total":      subtotal,
                            "forma_pago": forma_pago,
                        }])
                        df_v_updated = pd.concat([df_v, nueva_v], ignore_index=True)

                        nueva_vd = pd.DataFrame([{
                            "id_pedido":   nuevo_id,
                            "id_producto": id_p,
                            "producto":    prod_sel,
                            "cantidad":    cant,
                            "subtotal":    subtotal,
                        }])
                        df_vd_updated = pd.concat([df_vd, nueva_vd], ignore_index=True)

                        ok1 = _save("pedidos", df_v_updated)
                        ok2 = _save("pedido_detalle", df_vd_updated)

                        if ok1 and ok2:
                            st.success(f"Venta #{nuevo_id} registrada para {cliente_sel}")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error al registrar la venta: {e}")
                else:
                    st.warning("Por favor, seleccioná Cliente, Producto y una cantidad mayor a 0.")

    with col2:
        st.write("### Historial Agrupado por Fecha y Cliente")
        if not df_v.empty:
            df_v["fecha_dt"]  = pd.to_datetime(df_v["fecha"], errors="coerce")
            fecha_hoy         = datetime.now().strftime("%Y-%m-%d")
            df_v["fecha_dia"] = df_v["fecha_dt"].dt.strftime("%Y-%m-%d").fillna(fecha_hoy)

            dias_unicos = sorted(df_v["fecha_dia"].unique().tolist(), reverse=True)

            for dia in dias_unicos:
                with st.expander(f"📅 Fecha: {dia}"):
                    ventas_dia = df_v[df_v["fecha_dia"] == dia]

                    for cliente in ventas_dia["cliente"].unique():
                        st.markdown(f"#### 👤 Cliente: {cliente}")
                        ids_pedidos = ventas_dia[ventas_dia["cliente"] == cliente]["id_pedido"].unique()
                        detalle = df_vd[df_vd["id_pedido"].isin(ids_pedidos)][["producto", "cantidad", "subtotal"]].copy()
                        detalle["subtotal"] = pd.to_numeric(detalle["subtotal"], errors="coerce").fillna(0)
                        total_cliente = detalle["subtotal"].sum()

                        if not detalle.empty:
                            ids_str = ", ".join(map(str, ids_pedidos))
                            st.write(f"**Ventas #({ids_str})**")
                            detalle["cantidad"] = pd.to_numeric(detalle["cantidad"], errors="coerce").fillna(0).astype(int)
                            st.table(
                                detalle.rename(columns={
                                    "producto": "Producto",
                                    "cantidad": "Cant (gr)",
                                    "subtotal": "Subtotal",
                                }).style.format({
                                    "Cant (gr)": "{:d}",
                                    "Subtotal":  lambda x: f"$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                                })
                            )
                            total_f = f"$ {total_cliente:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                            st.write(f"**Total acumulado: {total_f}**")
                        st.write("---")
        else:
            st.info("No hay ventas registradas.")


# ─────────────────────────────────────────────
#  🛒 COMPRAS
# ─────────────────────────────────────────────
with tabs[1]:
    st.subheader("Gestión de Compras")
    df_p_c = _read("productos")
    df_pr  = _read("proveedores")
    df_c   = _read("compras")

    col1, col2 = st.columns([1, 2])

    with col1:
        st.write("### Registrar Nueva Compra")
        with st.form("form_compra", clear_on_submit=True):
            prod_lista = (
                df_p_c["producto"].dropna().unique().tolist()
                if not df_p_c.empty else []
            )
            p_sel = st.selectbox(
                "Producto", prod_lista,
                index=None, placeholder="Escribe para buscar el producto..."
            )

            prov_lista = (
                df_pr["nombre_proveedor"].dropna().unique().tolist()
                if not df_pr.empty else []
            )
            pr_sel = st.selectbox(
                "Proveedor", prov_lista,
                index=None, placeholder="Escribe para buscar el proveedor..."
            )

            c_cant     = st.number_input("Cantidad (grs)", min_value=0, step=100)
            c_costo_kg = st.number_input("Precio de Costo (x 1000 grs)", min_value=0.0, step=0.1)

            total_calculado = (c_cant / 1000) * c_costo_kg
            st.info(f"**Subtotal a registrar:** $ {total_calculado:,.2f}")

            if st.form_submit_button("Guardar Compra"):
                if p_sel and pr_sel and c_cant > 0:
                    try:
                        row_p    = df_p_c[df_p_c["producto"] == p_sel].iloc[0]
                        nueva_c  = pd.DataFrame([{
                            "id_compra":    _next_id(df_c, "id_compra"),
                            "id_producto":  row_p["id_producto"],
                            "producto":     p_sel,
                            "proveedor":    pr_sel,
                            "cantidad":     c_cant,
                            "precio_total": total_calculado,
                            "fecha":        datetime.now().strftime("%Y-%m-%d"),
                        }])
                        df_c_updated = pd.concat([df_c, nueva_c], ignore_index=True)

                        if _save("compras", df_c_updated):
                            st.success("Compra guardada correctamente.")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error al guardar la compra: {e}")
                else:
                    st.warning("Por favor, seleccioná producto, proveedor y una cantidad mayor a 0.")

    with col2:
        st.write("### Historial Agrupado por Fecha")
        df_c_hist = _read("compras")

        if not df_c_hist.empty:
            df_c_hist["precio_total"] = pd.to_numeric(df_c_hist["precio_total"], errors="coerce").fillna(0)

            resumen = (
                df_c_hist.groupby("fecha")
                .agg(precio_total=("precio_total", "sum"))
                .sort_index(ascending=False)
                .reset_index()
            )

            for _, row in resumen.iterrows():
                fecha_str = row["fecha"]
                total_dia = row["precio_total"]
                with st.expander(f"📅 {fecha_str}  |  Total: $ {total_dia:,.2f}"):
                    detalle_dia = df_c_hist[df_c_hist["fecha"] == fecha_str][
                        ["producto", "proveedor", "cantidad", "precio_total"]
                    ].copy()
                    detalle_dia["cantidad"] = pd.to_numeric(detalle_dia["cantidad"], errors="coerce").fillna(0).astype(int)
                    st.table(
                        detalle_dia.rename(columns={
                            "producto":     "Producto",
                            "proveedor":    "Proveedor",
                            "cantidad":     "Gramos",
                            "precio_total": "Subtotal",
                        }).style.format({
                            "Gramos":   "{:d}",
                            "Subtotal": lambda x: f"$ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                        })
                    )
        else:
            st.info("No hay compras registradas aún.")


# ─────────────────────────────────────────────
#  🍎 PRODUCTOS
# ─────────────────────────────────────────────
with tabs[2]:
    st.subheader("Gestión de Productos")

    col_form, col_tabla = st.columns([1, 2])

    with col_form:
        st.write("### Nuevo Producto")
        with st.form("form_producto", clear_on_submit=True):
            n_prod     = st.text_input("Nombre del Producto")
            n_costo    = st.number_input("Costo por kg", min_value=0.0)
            n_ganancia = st.number_input("% Ganancia", min_value=0.0, value=30.0)
            n_inicial  = st.number_input("Stock Inicial (grs)", min_value=0.0)

            if st.form_submit_button("Guardar Producto"):
                if n_prod.strip():
                    try:
                        df_prods_actual = _read("productos")
                        precio_venta    = n_costo * (1 + n_ganancia / 100)
                        nuevo_prod = pd.DataFrame([{
                            "id_producto":        _next_id(df_prods_actual, "id_producto"),
                            "producto":           n_prod.strip(),
                            "precio_compra":      n_costo,
                            "precio_venta":       round(precio_venta, 2),
                            "porcentaje_ganancia": n_ganancia,
                            "stock_inicial":      n_inicial,
                        }])
                        df_prods_updated = pd.concat([df_prods_actual, nuevo_prod], ignore_index=True)

                        if _save("productos", df_prods_updated):
                            st.success(f"Producto '{n_prod}' guardado. Precio de venta sugerido: $ {precio_venta:,.2f}")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error al guardar el producto: {e}")
                else:
                    st.warning("El nombre del producto no puede estar vacío.")

    with col_tabla:
        st.write("### Listado de Productos")
        df_prod = obtener_df_con_stock()

        if not df_prod.empty:
            df_prod_edit = st.data_editor(
                df_prod,
                use_container_width=True,
                key="editor_productos",
                column_config={
                    "precio_compra":       st.column_config.NumberColumn("Costo",              format="$ %.2f"),
                    "precio_venta":        st.column_config.NumberColumn("Precio Venta",       format="$ %.2f"),
                    "porcentaje_ganancia": st.column_config.NumberColumn("% Ganancia",         format="%.2f %%"),
                    "stock_inicial":       st.column_config.NumberColumn("Stock Inicial (grs)", format="%d"),
                    "stock_actual":        st.column_config.NumberColumn("Stock Actual (grs)",  format="%d"),
                },
            )

            col_pdf, col_save = st.columns(2)
            with col_pdf:
                pdf_bytes = generar_pdf_precios(df_prod_edit.copy())
                st.download_button(
                    label="📥 Descargar Lista de Precios PDF",
                    data=pdf_bytes,
                    file_name="Lista_Precios.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
            with col_save:
                if st.button("💾 Guardar cambios de productos", use_container_width=True):
                    # Guardamos sin la columna calculada stock_actual
                    cols_a_guardar = [c for c in df_prod_edit.columns if c != "stock_actual"]
                    if _save("productos", df_prod_edit[cols_a_guardar]):
                        st.success("Cambios guardados.")
                        st.rerun()
        else:
            st.error("No se pudieron cargar los datos de productos.")


# ─────────────────────────────────────────────
#  📦 STOCK
# ─────────────────────────────────────────────
with tabs[3]:
    st.subheader("Estado de Inventario")
    df_stock = obtener_df_con_stock()

    if not df_stock.empty:
        cols_stock = [c for c in ["producto", "stock_inicial", "stock_actual"] if c in df_stock.columns]
        df_display = df_stock[cols_stock].copy()
        df_display["stock_actual"] = pd.to_numeric(df_display["stock_actual"], errors="coerce").fillna(0)

        # Alerta visual para productos con stock bajo
        umbral = st.number_input("⚠️ Umbral de stock bajo (grs)", min_value=0, value=500, step=100)
        bajo_stock = df_display[df_display["stock_actual"] < umbral]
        if not bajo_stock.empty:
            st.warning(f"Hay {len(bajo_stock)} producto(s) con stock por debajo de {umbral} grs.")

        # Convertimos stocks a entero para mostrar sin decimales
        for col_s in ["stock_inicial", "stock_actual"]:
            if col_s in df_display.columns:
                df_display[col_s] = pd.to_numeric(df_display[col_s], errors="coerce").fillna(0).astype(int)

        st.dataframe(
            df_display.style.map(
                lambda v: "background-color: #e05555; color: #ffffff; font-weight: bold" if isinstance(v, (int, float)) and v < umbral else "",
                subset=["stock_actual"],
            ),
            use_container_width=True,
            hide_index=True,
            column_config={
                "producto":      "Producto",
                "stock_inicial": st.column_config.NumberColumn("Stock Inicial (grs)", format="%d"),
                "stock_actual":  st.column_config.NumberColumn("Stock Actual (grs)",  format="%d"),
            },
        )
    else:
        st.error("No se pudieron cargar los datos de stock.")


# ─────────────────────────────────────────────
#  👥 CLIENTES
# ─────────────────────────────────────────────
with tabs[4]:
    st.subheader("Gestión de Clientes")
    df_cl = _read("clientes")

    COLS_CLIENTES = ["id_cliente", "nombre_apellido", "celular", "direccion"]
    df_cl = df_cl[[c for c in COLS_CLIENTES if c in df_cl.columns]]

    if not df_cl.empty and "celular" in df_cl.columns:
        df_cl["celular"] = (
            df_cl["celular"].astype(str)
            .str.replace(r"\.0$", "", regex=True)
            .replace("nan", "")
        )

    c1, c2 = st.columns([1, 2])

    with c1:
        st.write("### Registrar Nuevo Cliente")
        with st.form("form_cliente", clear_on_submit=True):
            nom        = st.text_input("Nombre y Apellido")
            cel        = st.text_input("Celular / WhatsApp")
            direccion  = st.text_input("Dirección")   # ← corregido: ya no pisa el builtin 'dir'

            if st.form_submit_button("Registrar Cliente"):
                if nom.strip():
                    try:
                        nuevo_id = _next_id(df_cl, "id_cliente")
                        nuevo_cl = pd.DataFrame([{
                            "id_cliente":      nuevo_id,
                            "nombre_apellido": nom.strip(),
                            "celular":         cel,
                            "direccion":       direccion,
                        }])
                        df_final = pd.concat([df_cl, nuevo_cl], ignore_index=True)

                        if _save("clientes", df_final[COLS_CLIENTES]):
                            st.success(f"Cliente '{nom}' guardado.")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error al registrar el cliente: {e}")
                else:
                    st.warning("El nombre no puede estar vacío.")

    with c2:
        st.write("### Listado y Edición de Clientes")
        if not df_cl.empty:
            df_editado = st.data_editor(
                df_cl,
                column_config={
                    "id_cliente":      st.column_config.NumberColumn("ID", disabled=True),
                    "nombre_apellido": "Nombre y Apellido",
                    "celular":         "Celular",
                    "direccion":       "Dirección",
                },
                use_container_width=True,
                hide_index=True,
                key="editor_clientes",
            )
            if st.button("💾 Guardar Cambios en la Lista de Clientes"):
                cols_guardar = [c for c in COLS_CLIENTES if c in df_editado.columns]
                if _save("clientes", df_editado[cols_guardar]):
                    st.success("¡Lista de clientes actualizada!")
                    st.rerun()
        else:
            st.info("No hay clientes registrados.")


# ─────────────────────────────────────────────
#  🚛 PROVEEDORES
# ─────────────────────────────────────────────
with tabs[5]:
    st.subheader("Gestión de Proveedores")
    df_pr = _read("proveedores")

    COLS_PROVEEDORES = ["id_proveedor", "nombre_proveedor", "contacto", "direccion"]
    df_pr_limpio = df_pr[[c for c in COLS_PROVEEDORES if c in df_pr.columns]]

    p1, p2 = st.columns([1, 2])

    with p1:
        st.write("### Registrar Proveedor")
        with st.form("form_prov", clear_on_submit=True):
            n_prov    = st.text_input("Nombre de la Empresa / Proveedor")
            c_prov    = st.text_input("Contacto / Vendedor")
            dir_prov  = st.text_input("Dirección")   # ← corregido: ya no pisa el builtin 'dir'

            if st.form_submit_button("Registrar Proveedor"):
                if n_prov.strip():
                    try:
                        nuevo_pr = pd.DataFrame([{
                            "id_proveedor":    _next_id(df_pr_limpio, "id_proveedor"),
                            "nombre_proveedor": n_prov.strip(),
                            "contacto":        c_prov,
                            "direccion":       dir_prov,
                        }])
                        df_final = pd.concat([df_pr_limpio, nuevo_pr], ignore_index=True)

                        if _save("proveedores", df_final):
                            st.success(f"Proveedor '{n_prov}' guardado.")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error al registrar el proveedor: {e}")
                else:
                    st.warning("El nombre del proveedor no puede estar vacío.")

    with p2:
        st.write("### Listado de Proveedores")
        if not df_pr_limpio.empty:
            st.dataframe(
                df_pr_limpio,
                use_container_width=True,
                hide_index=True,
                column_config={"id_proveedor": "ID"},
            )
        else:
            st.info("No hay proveedores registrados.")
