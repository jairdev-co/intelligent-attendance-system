# Sistema de Asistencia Inteligente

Aplicación web de control de asistencia por reconocimiento facial de la Universidad CESMAG. Un PC con cámara muestra la pantalla de asistencia, que reconoce a la persona y registra su entrada o salida con un clic. Desde un panel protegido, el administrador registra a las personas, consulta la asistencia y la exporta a CSV.

## Stack

| Capa                  | Tecnología                                             |
| --------------------- | ------------------------------------------------------ |
| Backend               | Python 3.12, Django 5.2, Gunicorn                      |
| Reconocimiento facial | face_recognition 1.3 (dlib 20), OpenCV, NumPy          |
| Base de datos         | PostgreSQL en producción, SQLite en local              |
| UI                    | Plantillas de Django, Bootstrap 5, JavaScript          |
| Estáticos             | WhiteNoise                                             |
| Despliegue            | Railway (Dockerfile + PostgreSQL)                      |

## Características

- **Pantalla de asistencia**: busca rostros cada ~4 s; al reconocer a alguien muestra su nombre y los botones **Entrada** y **Salida**. Al pulsar un botón toma una foto nueva y no registra nada si la persona cambió.
- **Reconocimiento confiable**: rechaza fotos con más de un rostro y coincidencias ambiguas entre dos personas.
- **Sin duplicados**: una persona no puede marcar el mismo tipo de asistencia dos veces en 60 s.
- **Registro de personas**: nombre, número de identificación, tipo de usuario y foto desde la cámara. No permite repetir rostro, nombre ni identificación.
- **Autorización de datos**: el rostro solo se guarda si la persona acepta los términos y condiciones (Ley 1581 de 2012).
- **Gestión de usuarios**: listar con foto, editar datos o foto y eliminar.
- **Panel de asistencia**: filtros por tipo de usuario y rango de fechas, y descarga en `.csv` de todos los registros filtrados.
- **Paginación**: el panel muestra 25 registros por página (se puede elegir 25, 50 o 100) con los botones **Anterior** y **Siguiente**. Al cambiar de página o eliminar un registro se mantienen los filtros y la página actual.
- **Seguridad**: protección CSRF, límite de 60 peticiones por minuto en la pantalla de asistencia y de 10 intentos de login cada 5 min por IP, sesiones que se cierran tras 15 min sin actividad, 8 horas después del inicio de sesión o al cerrar el navegador, y cookies seguras en producción.

## Estructura

```
intelligent-attendance-system/
├── accounts/
│   ├── models.py          # Modelos UserImages y Attendance
│   ├── views.py           # Vistas
│   ├── utils.py           # Reconocimiento facial y límite de peticiones
│   ├── templates/         # Plantillas HTML
│   └── static/            # CSS, JavaScript e imágenes
├── face_auth/             # Configuración del proyecto
├── Dockerfile             # Imagen que construye Railway
├── railway.json           # Le indica a Railway que use el Dockerfile
├── Procfile               # Comando de arranque sin Docker
├── requirements.txt
└── manage.py
```

## Requisitos

- Python 3.12
- `cmake` y un compilador de C++ para instalar dlib (Debian/Ubuntu: `sudo apt install build-essential cmake python3-dev`)
- Una cuenta de Railway para producción

## Instalación

```bash
git clone https://github.com/jairdev-co/intelligent-attendance-system.git
cd intelligent-attendance-system
python3.12 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

La instalación compila dlib y tarda varios minutos.

## Configuración

Crea un archivo `.env` en la raíz del proyecto a partir de `.env.example`:

```env
DJANGO_SECRET_KEY=una_clave_larga_y_aleatoria
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=
```

| Variable                      | Descripción                                                      |
| ----------------------------- | ---------------------------------------------------------------- |
| `DJANGO_SECRET_KEY`           | Firma las sesiones. Sin ella la aplicación no arranca            |
| `DJANGO_DEBUG`                | `True` solo en local. Por defecto `False`                        |
| `DJANGO_ALLOWED_HOSTS`        | Dominios o IP permitidos, separados por comas                    |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Orígenes HTTPS de confianza, separados por comas                 |
| `DATABASE_URL`                | Conexión a PostgreSQL. Si falta, usa SQLite                      |
| `RAILWAY_PUBLIC_DOMAIN`       | La pone Railway; se agrega solo a los hosts y orígenes permitidos |

Genera la `DJANGO_SECRET_KEY` con:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

### Base de datos

Crea las tablas:

```bash
python manage.py migrate
```

### Primer administrador

El panel y el registro de personas solo están disponibles para administradores, así que el primero se crea por consola:

```bash
python manage.py createsuperuser
```

## Uso

```bash
python manage.py runserver
```

Pantalla de asistencia en <http://localhost:8000> y panel en <http://localhost:8000/login/>.

El navegador solo permite usar la cámara en `localhost` o con HTTPS.

### Pruebas

```bash
python manage.py test accounts
```

Django crea y borra su propia base de pruebas; el reconocimiento facial se simula, así que no hace falta cámara.

### Despliegue

El sistema se despliega en [Railway](https://railway.com) con el `Dockerfile` del repositorio, que compila dlib, aplica las migraciones y sirve la aplicación con Gunicorn. Railway da HTTPS, que el navegador exige para usar la cámara.

1. En Railway crea un proyecto con **Deploy from GitHub repo** y elige este repositorio.
2. En el mismo proyecto agrega una base de datos con **+ New → Database → PostgreSQL**.
3. En el servicio de la aplicación, pestaña **Variables**, agrega:

   | Variable            | Valor                                  |
   | ------------------- | -------------------------------------- |
   | `DJANGO_SECRET_KEY` | Una clave nueva (ver Configuración)    |
   | `DJANGO_DEBUG`      | `False`                                |
   | `DATABASE_URL`      | `${{Postgres.DATABASE_URL}}`           |

4. En **Settings → Networking** pulsa **Generate Domain**. La aplicación acepta ese dominio sola.
5. Cuando el despliegue termine, crea el primer administrador desde la [CLI de Railway](https://docs.railway.com/guides/cli):

   ```bash
   railway link
   railway ssh
   python manage.py createsuperuser
   ```

Cada `git push` a la rama principal vuelve a desplegar. Las migraciones se aplican solas al arrancar. La primera construcción tarda varios minutos porque compila dlib.

## Rutas

| Método    | Ruta                       | Descripción                                       | Auth |
| --------- | -------------------------- | ------------------------------------------------- | ---- |
| GET       | `/`                        | Pantalla de asistencia                            | No   |
| POST      | `/`                        | Identifica el rostro y registra la asistencia     | No   |
| GET       | `/terms/`                  | Términos y condiciones                            | No   |
| GET, POST | `/login/`                  | Inicio de sesión                                  | No   |
| GET       | `/main/`                   | Panel de asistencia paginado y descarga del CSV   | Sí   |
| POST      | `/attendance/remove/<id>/` | Elimina un registro de asistencia                 | Sí   |
| GET       | `/users/`                  | Listado de personas                               | Sí   |
| GET, POST | `/register/`               | Registra una persona                              | Sí   |
| GET, POST | `/user/update/<id>/`       | Edita una persona                                 | Sí   |
| GET       | `/user/photo/<id>/`        | Foto de una persona                               | Sí   |
| POST      | `/user/delete/<id>/`       | Elimina una persona y sus registros               | Sí   |
| GET, POST | `/admin/`                  | Administración de Django                          | Sí   |
| POST      | `/logout/`                 | Cierra la sesión                                  | Sí   |

## Autor

Brayan Jair Enriquez
