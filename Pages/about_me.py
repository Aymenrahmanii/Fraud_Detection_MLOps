import streamlit as st

def about_me_page():
    # Title
    st.title("👨‍💻 About Me - Mohit Gupta")

    # Intro
    st.markdown("""
    Hi! I'm **Mohit Gupta**, a passionate **AI/ML Engineer** with expertise in building **end-to-end machine learning products** 
    and specializing in **Generative AI, MLOps, and Deep Learning**.  
    
    
    I enjoy transforming ideas into production-ready solutions using modern AI tools and frameworks.
    
    ---
    """)

    # Technical Skills
    st.subheader("⚡ Technical Skills")
    st.markdown("""
    - **Languages:** Python, C++  
    - **Developer Tools:** HTML/CSS, Tailwind, Flask, GitHub, SQL, MongoDB, OOPS, DBMS  
    - **ML & Data Science:** Machine Learning & Deep Learning Algorithms, AutoML, Data Analysis,  
      Power BI, Tableau, PyTorch, TensorFlow, NLP, RAG, LangChain, LangGraph, ChromaDB, CrewAI, AutoGen, OpenCV,  
      Time Series Forecasting, LLM Finetuning, Generative AI, Diffusion Models  
    - **MLOps & Cloud:** Docker, MLflow, Amazon AWS S3, Kubernetes, Prometheus, Grafana, FastAPI
    
    ---
    """)

    # Projects Section
    st.subheader("🚀 Featured Projects")
    st.markdown("""
    - **[Fashion Sense AI](https://fashion-sense-ai.streamlit.app/)** – Hybrid fashion product retrieval & recommendation using CLIP, FAISS, and Gemma.  
    - **[Corn Vomitoxin Prediction](https://github.com/MohitGupta0123/Corn_vomitoxin_ppb_prediction)** – PyTorch + Dockerized Flask API predicting toxins in corn using hyperspectral data.  
    
    ---
    """)

    # Links
    st.subheader("🌐 Connect with Me")
    st.markdown("""
    - **LinkedIn:** [linkedin.com/in/mohitgupta012](https://www.linkedin.com/in/mohitgupta012)  
    - **GitHub:** [github.com/MohitGupta0123](https://github.com/MohitGupta0123)  
    """)
